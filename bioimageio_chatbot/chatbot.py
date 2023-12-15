import asyncio
import os
import json
import datetime
import secrets
import aiofiles
from imjoy_rpc.hypha import login, connect_to_server

from pydantic import BaseModel, Field
from schema_agents.role import Role
from schema_agents.schema import Message
from typing import Any, Dict, List, Optional, Union
import requests
import sys
import io
from bioimageio_chatbot.knowledge_base import load_knowledge_base
from bioimageio_chatbot.utils import get_manifest
import pkg_resources
import base64
from bioimageio_chatbot.jsonschema_pydantic import jsonschema_to_pydantic, JsonSchemaObject

def decode_base64(encoded_data):
    decoded_data = base64.b64decode(encoded_data.split(',')[1])
    return decoded_data
    
def load_model_info():
    response = requests.get("https://bioimage-io.github.io/collection-bioimage-io/collection.json")
    assert response.status_code == 200
    model_info = response.json()
    resource_items = model_info['collection']
    return resource_items

def execute_code(script, context=None):
    if context is None:
        context = {}

    # Redirect stdout and stderr to capture their output
    original_stdout = sys.stdout
    original_stderr = sys.stderr
    sys.stdout = io.StringIO()
    sys.stderr = io.StringIO()

    try:
        # Create a copy of the context to avoid modifying the original
        local_vars = context.copy()

        # Execute the provided Python script with access to context variables
        exec(script, local_vars)

        # Capture the output from stdout and stderr
        stdout_output = sys.stdout.getvalue()
        stderr_output = sys.stderr.getvalue()

        return {
            "stdout": stdout_output,
            "stderr": stderr_output,
            # "context": local_vars  # Include context variables in the result
        }
    except Exception as e:
        return {
            "stdout": "",
            "stderr": str(e),
            # "context": context  # Include context variables in the result even if an error occurs
        }
    finally:
        # Restore the original stdout and stderr
        sys.stdout = original_stdout
        sys.stderr = original_stderr


class ModelZooInfoScriptResults(BaseModel):
    """Results of executing a model zoo info query script."""
    stdout: str = Field(description="The query script execution stdout output.")
    stderr: str = Field(description="The query script execution stderr output.")
    request: str = Field(description="Details concerning the user's request that triggered the model zoo query script execution")
    user_info: Optional[str] = Field("", description="The user's info for personalizing response.")

class DirectResponse(BaseModel):
    """Direct response to a user's question if you are confident about the answer."""
    response: str = Field(description="The response to the user's question answering what that asked for.")

class LearnResponse(BaseModel):
    """Pedagogical response to user's question if the user's question is related to learning, the response should include such as key terms and important concepts about the topic."""
    response: str = Field(description="The response to user's question, make sure the response is pedagogical and educational.")
    user_info: Optional[str] = Field("", description="The user's info for personalizing response.")
    
class DocWithScore(BaseModel):
    """A document with an associated relevance score."""
    doc: str = Field(description="The document retrieved.")
    score: float = Field(description="The relevance score of the retrieved document.")
    metadata: Dict[str, Any] = Field(description="The document's metadata.")
    base_url: Optional[str] = Field(None, description="The documentation's base URL, which will be used to resolve the relative URLs in the retrieved document chunks when producing markdown links.")

class DocumentSearchInput(BaseModel):
    """Results of a document retrieval process from a documentation base."""
    user_question: str = Field(description="The user's original question.")
    relevant_context: List[DocWithScore] = Field(description="Chunks of context retrieved from the documentation that are relevant to the user's original question.")
    user_info: Optional[str] = Field("", description="The user's info for personalizing the response.")
    format: Optional[str] = Field(None, description="The format of the document.")
    preliminary_response: Optional[str] = Field(None, description="The preliminary response to the user's question. This will be combined with the retrieved documents to produce the final response.")
    
class FinalResponse(BaseModel):
    """The final response to the user's question based on the preliminary response and the documentation search results. 
    If the documentation search results are relevant to the user's question, provide a text response to the question based on the search results.
    If the documentation search results contains only low relevance scores or if the question isn't relevant to the search results, return the preliminary response.
    Importantly, if you can't confidently provide a relevant response to the user's question, return 'Sorry I didn't find relevant information in model zoo, please try again.'."""
    response: str = Field(description="The answer to the user's question based on the search results. Can be either a detailed response in markdown format if the search results are relevant to the user's question or 'I don't know'.")

class ChannelInfo(BaseModel):
    """The selected knowledge base channel for the user's question. If provided, rely only on the selected channel when answering the user's question."""
    id: str = Field(description="The channel id.")
    name: str = Field(description="The channel name.")
    description: str = Field(description="The channel description.")

class UserProfile(BaseModel):
    """The user's profile. This will be used to personalize the response to the user."""
    name: str = Field(description="The user's name.", max_length=32)
    occupation: str = Field(description="The user's occupation.", max_length=128)
    background: str = Field(description="The user's background.", max_length=256)

class QuestionWithHistory(BaseModel):
    """The user's question, chat history, and user's profile."""
    question: str = Field(description="The user's question.")
    chat_history: Optional[List[Dict[str, str]]] = Field(None, description="The chat history.")
    user_profile: Optional[UserProfile] = Field(None, description="The user's profile. You should use this to personalize the response based on the user's background and occupation.")
    channel_info: Optional[ChannelInfo] = Field(None, description="The selected channel of the user's question. If provided, rely only on the selected channel when answering the user's question.")
    custom_functions: Optional[Dict[str, Any]] = Field(None, description="Custom functions provided from the client.")
class ResponseStep(BaseModel):
    """Response step"""
    name: str = Field(description="Step name")
    details: Optional[dict] = Field(None, description="Step details")

class RichResponse(BaseModel):
    """Rich response with text and intermediate steps"""
    text: str = Field(description="Response text")
    steps: List[ResponseStep] = Field(description="Intermediate steps")

def create_customer_service(db_path):
    debug = os.environ.get("BIOIMAGEIO_DEBUG") == "true"
    collections = get_manifest()['collections']
    docs_store_dict = load_knowledge_base(db_path)
    collection_info_dict = {collection['id']: collection for collection in collections}
    resource_items = load_model_info()
    types = set()
    tags = set()
    for resource in resource_items:
        types.add(resource['type'])
        tags.update(resource['tags'])
    types = list(types)
    tags = list(tags)[:10]
    
    channels_info = "\n".join(f"""- `{collection['id']}`: {collection['description']}""" for collection in collections)
    resource_item_stats = f"""Each item contains the following fields: {list(resource_items[0].keys())}\nThe available resource types are: {types}\nSome example tags: {tags}\nHere is an example: {resource_items[0]}"""
    class DocumentRetrievalInput(BaseModel):
        """Input for searching knowledge bases and finding documents relevant to the user's request."""
        request: str = Field(description="The user's detailed request")
        preliminary_response: str = Field(description="The preliminary response to the user's question. This will be combined with the retrieved documents to produce the final response.")
        query: str = Field(description="The query used to retrieve documents related to the user's request. Take preliminary_response as reference to generate query if needed.")
        user_info: Optional[str] = Field("", description="Brief user info summary including name, background, etc., for personalizing responses to the user.")
        channel_id: str = Field(description=f"The channel_id of the knowledge base to search. It MUST be the same as the user provided channel_id. If not specified, either select 'all' to search through all the available knowledge base channels or select one from the available channels which are:\n{channels_info}. If you are not sure which channel to select, select 'all'.")
        
    class ModelZooInfoScript(BaseModel):
        """Create a Python Script to get information about details of models, applications, datasets, etc. in the model zoo. Remember some items in the model zoo may not have all the fields, so you need to handle the missing fields."""
        script: str = Field(description="The script to be executed which uses a predefined local variable `resources` containing a list of dictionaries with all the resources in the model zoo (including models, applications, datasets etc.). The response to the query should be printed to stdout. Details about the local variable `resources`:\n" + resource_item_stats)
        request: str = Field(description="The user's detailed request")
        user_info: Optional[str] = Field("", description="Brief user info summary including name, background, etc., for personalizing responses to the user.")

    async def respond_to_user(question_with_history: QuestionWithHistory = None, role: Role = None) -> str:
        """Answer the user's question directly or retrieve relevant documents from the documentation, or create a Python Script to get information about details of models."""
        steps = []
        inputs = [question_with_history.user_profile] + list(question_with_history.chat_history) + [question_with_history.question]
        # The channel info will be inserted at the beginning of the inputs
        if question_with_history.channel_info:
            inputs.insert(0, question_with_history.channel_info)

        if question_with_history.custom_functions and question_with_history.channel_info is not None and question_with_history.channel_info.id == "function-call":
            try:
                func = question_with_history.custom_functions[0]
                req = await role.aask(inputs,func['input_type'])
                steps.append(ResponseStep(name="Function Call", details=req.dict()))
                await func['execute'](**req.dict())
            except Exception as e:
                # try again
                req = await role.aask(inputs, LearnResponse)
                steps.append(ResponseStep(name="Learn"))
            return RichResponse(text=req.response, steps=steps)

        if question_with_history.channel_info is not None and question_with_history.channel_info.id == "learn":
            try:
                req = await role.aask(inputs, LearnResponse)
                steps.append(ResponseStep(name="Learn", details=req.dict()))
            except Exception as e:
                # try again
                req = await role.aask(inputs, LearnResponse)
                steps.append(ResponseStep(name="Learn"))
            return RichResponse(text=req.response, steps=steps)
            
        if not question_with_history.channel_info or question_with_history.channel_info.id == "bioimage.io":
            try:
                req = await role.aask(inputs, Union[DirectResponse, DocumentRetrievalInput, ModelZooInfoScript, LearnResponse])
            except Exception as e:
                # try again
                req = await role.aask(inputs, Union[DirectResponse, DocumentRetrievalInput, ModelZooInfoScript, LearnResponse])
        else:
            try:
                req = await role.aask(inputs, Union[DirectResponse, DocumentRetrievalInput])
            except Exception as e:
                # try again
                req = await role.aask(inputs, Union[DirectResponse, DocumentRetrievalInput])
        if isinstance(req, DirectResponse):
            steps.append(ResponseStep(name="Direct"))
            return RichResponse(text=req.response, steps=steps)
        elif isinstance(req, LearnResponse):
            steps.append(ResponseStep(name="Learn"))
            return RichResponse(text=req.response, steps=steps)
        elif isinstance(req, DocumentRetrievalInput):
            steps.append(ResponseStep(name="Document Retrieval", details= req.dict()))
            if req.channel_id == "all":
                docs_with_score = []
                # loop through all the channels
                for channel_id in docs_store_dict.keys():
                    docs_store = docs_store_dict[channel_id]
                    collection_info = collection_info_dict[channel_id]
                    base_url = collection_info.get('base_url')
                    print(f"Retrieving documents from database {channel_id} with query: {req.query}")
                    results_with_scores = await docs_store.asimilarity_search_with_relevance_scores(req.query, k=2)
                    new_docs_with_score = [DocWithScore(doc=doc.page_content, score=score, metadata=doc.metadata, base_url=base_url) for doc, score in results_with_scores]
                    docs_with_score.extend(new_docs_with_score)
                    print(f"Retrieved documents:\n{new_docs_with_score[0].doc[:20] + '...'} (score: {new_docs_with_score[0].score})\n{new_docs_with_score[1].doc[:20] + '...'} (score: {new_docs_with_score[1].score})")
                # rank the documents by relevance score
                docs_with_score = sorted(docs_with_score, key=lambda x: x.score, reverse=True)
                # only keep the top 3 documents
                docs_with_score = docs_with_score[:3]
                search_input = DocumentSearchInput(user_question=req.request, relevant_context=docs_with_score, user_info=req.user_info, format=None, preliminary_response=req.preliminary_response)
                steps.append(ResponseStep(name="Document Search", details=search_input.dict()))
                response = await role.aask(search_input, FinalResponse)
                steps.append(ResponseStep(name="Final Response", details=response.dict()))
            else:
                docs_store = docs_store_dict[req.channel_id]
                collection_info = collection_info_dict[req.channel_id]
                base_url = collection_info.get('base_url')
                print(f"Retrieving documents from database {req.channel_id} with query: {req.query}")
                results_with_scores = await docs_store.asimilarity_search_with_relevance_scores(req.query, k=3)
                docs_with_score = [DocWithScore(doc=doc.page_content, score=score, metadata=doc.metadata, base_url=base_url) for doc, score in results_with_scores]
                print(f"Retrieved documents:\n{docs_with_score[0].doc[:20] + '...'} (score: {docs_with_score[0].score})\n{docs_with_score[1].doc[:20] + '...'} (score: {docs_with_score[1].score})\n{docs_with_score[2].doc[:20] + '...'} (score: {docs_with_score[2].score})")
                search_input = DocumentSearchInput(user_question=req.request, relevant_context=docs_with_score, user_info=req.user_info, format=collection_info.get('format'), preliminary_response=req.preliminary_response)
                steps.append(ResponseStep(name="Document Search", details=search_input.dict()))
                response = await role.aask(search_input, FinalResponse)
                steps.append(ResponseStep(name="Final Response", details=response.dict()))
            # source: doc.metadata.get('source', 'N/A')
            return RichResponse(text=response.response, steps=steps)
        elif isinstance(req, ModelZooInfoScript):
            steps.append(ResponseStep(name="Model Zoo Info Script", details=req.dict()))
            loop = asyncio.get_running_loop()
            print(f"Executing the script:\n{req.script}")
            result = await loop.run_in_executor(None, execute_code, req.script, {"resources": resource_items})
            print(f"Script execution result:\n{result}")
            steps.append(ResponseStep(name="Script Execution", details=result))
            response = await role.aask(ModelZooInfoScriptResults(
                stdout=result["stdout"],
                stderr=result["stderr"],
                request=req.request,
                user_info=req.user_info
            ), FinalResponse)
            steps.append(ResponseStep(name="Final Response", details=response.dict()))
            return RichResponse(text=response.response, steps=steps)
        
    customer_service = Role(
        name="Melman",
        profile="Customer Service",
        goal="Your goal as Melman from Madagascar, the community knowledge base manager, is to assist users in effectively utilizing the knowledge base for bioimage analysis. You are responsible for answering user questions, providing clarifications, retrieving relevant documents, and executing scripts as needed. Your overarching objective is to make the user experience both educational and enjoyable.",
        constraints=None,
        actions=[respond_to_user],
    )
    return customer_service

async def save_chat_history(chat_log_full_path, chat_his_dict):    
    # Serialize the chat history to a json string
    chat_history_json = json.dumps(chat_his_dict)

    # Write the serialized chat history to the json file
    async with aiofiles.open(chat_log_full_path, mode='w', encoding='utf-8') as f:
        await f.write(chat_history_json)

    
async def connect_server(server_url):
    """Connect to the server and register the chat service."""
    login_required = os.environ.get("BIOIMAGEIO_LOGIN_REQUIRED") == "true"
    if login_required:
        token = await login({"server_url": server_url})
    else:
        token = None
    server = await connect_to_server({"server_url": server_url, "token": token, "method_timeout": 100})
    await register_chat_service(server)
    
async def register_chat_service(server):
    """Hypha startup function."""
    collections = get_manifest()['collections']
    login_required = os.environ.get("BIOIMAGEIO_LOGIN_REQUIRED") == "true"
    knowledge_base_path = os.environ.get("BIOIMAGEIO_KNOWLEDGE_BASE_PATH", "./bioimageio-knowledge-base")
    assert knowledge_base_path is not None, "Please set the BIOIMAGEIO_KNOWLEDGE_BASE_PATH environment variable to the path of the knowledge base."
    if not os.path.exists(knowledge_base_path):
        print(f"The knowledge base is not found at {knowledge_base_path}, will download it automatically.")
        os.makedirs(knowledge_base_path, exist_ok=True)

    chat_logs_path = os.environ.get("BIOIMAGEIO_CHAT_LOGS_PATH", "./chat_logs")
    assert chat_logs_path is not None, "Please set the BIOIMAGEIO_CHAT_LOGS_PATH environment variable to the path of the chat logs folder."
    if not os.path.exists(chat_logs_path):
        print(f"The chat session folder is not found at {chat_logs_path}, will create one now.")
        os.makedirs(chat_logs_path, exist_ok=True)
    
    channel_id_by_name = {collection['name']: collection['id'] for collection in collections}
    description_by_id = {collection['id']: collection['description'] for collection in collections}
    customer_service = create_customer_service(knowledge_base_path)
    
    event_bus = customer_service.get_event_bus()
    event_bus.register_default_events()
        
    def load_authorized_emails():
        if login_required:
            authorized_users_path = os.environ.get("BIOIMAGEIO_AUTHORIZED_USERS_PATH")
            if authorized_users_path:
                assert os.path.exists(authorized_users_path), f"The authorized users file is not found at {authorized_users_path}"
                with open(authorized_users_path, "r") as f:
                    authorized_users = json.load(f)["users"]
                authorized_emails = [user["email"] for user in authorized_users if "email" in user]
            else:
                authorized_emails = None
        else:
            authorized_emails = None
        return authorized_emails

    authorized_emails = load_authorized_emails()
    def check_permission(user):
        if authorized_emails is None or user["email"] in authorized_emails:
            return True
        else:
            return False
        
    async def report(user_report, context=None):
        if login_required and context and context.get("user"):
            assert check_permission(context.get("user")), "You don't have permission to report the chat history."
        # get the chatbot version
        version = pkg_resources.get_distribution('bioimageio-chatbot').version
        chat_his_dict = {'type':user_report['type'],
                         'feedback':user_report['feedback'],
                         'conversations':user_report['messages'], 
                         'session_id':user_report['session_id'], 
                        'timestamp': str(datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")), 
                        'user': context.get('user'),
                        'version': version}
        session_id = user_report['session_id'] + secrets.token_hex(4)
        filename = f"report-{session_id}.json"
        # Create a chat_log.json file inside the session folder
        chat_log_full_path = os.path.join(chat_logs_path, filename)
        await save_chat_history(chat_log_full_path, chat_his_dict)
        print(f"User report saved to {filename}")
        
    async def chat(text, chat_history, user_profile=None, channel=None, status_callback=None, session_id=None, image_data=None, custom_functions=None, context=None):
        if login_required and context and context.get("user"):
            assert check_permission(context.get("user")), "You don't have permission to use the chatbot, please sign up and wait for approval"
        session_id = session_id or secrets.token_hex(8)
        # Listen to the `stream` event
        async def stream_callback(message):
            if message.type in ["function_call", "text"]:
                if message.session.id == session_id:
                    await status_callback(message.dict())

        event_bus.on("stream", stream_callback)


        if image_data:
            await status_callback({"type": "text", "content": f"\n![Uploaded Image]({image_data})\n"})
            try:
                decoded_image_data = decode_base64(image_data)
            except Exception as e:
                return f"Failed to decode the image, error: {e}"

        # Get the channel id by its name
        if channel == 'learn':
            channel_id = 'learn'
        elif channel == 'function-call':
            channel_id = 'function-call'
        else:
            if channel == 'auto':
                channel = None
            if channel:
                assert channel in channel_id_by_name, f"Channel {channel} is not found, available channels are {list(channel_id_by_name.keys())}"
                channel_id = channel_id_by_name[channel]
            else:
                channel_id = None
        if channel == 'learn':
            channel_info = {"id": channel_id, "name": channel, "description": "Learning assistant"}
        elif channel == 'function-call':
            channel_info = {"id": channel_id, "name": channel, "description": "Function call assistant"}
        else:
            channel_info = channel_id and {"id": channel_id, "name": channel, "description": description_by_id[channel_id]}
        if channel_info:
            channel_info = ChannelInfo.parse_obj(channel_info)

        for custom_function in custom_functions:
            input_type = jsonschema_to_pydantic(JsonSchemaObject.parse_obj(custom_function['schema']))
            custom_function['input_type'] = input_type

        # user_profile = {"name": "lulu", "occupation": "data scientist", "background": "machine learning and AI"}
        m = QuestionWithHistory(question=text, chat_history=chat_history, user_profile=UserProfile.parse_obj(user_profile),channel_info=channel_info, custom_functions=custom_functions)
        try:
            response = await customer_service.handle(Message(content=m.json(), data=m , role="User", session_id=session_id))
            # get the content of the last response
            response = response[-1].data # type: RichResponse
            print(f"\nUser: {text}\nChatbot: {response.text}")
        except Exception as e:
            event_bus.off("stream", stream_callback)
            raise e
        else:
            event_bus.off("stream", stream_callback)

        if session_id:
            chat_history.append({ 'role': 'user', 'content': text })
            chat_history.append({ 'role': 'assistant', 'content': response.text })
            version = pkg_resources.get_distribution('bioimageio-chatbot').version
            chat_his_dict = {'conversations':chat_history, 
                     'timestamp': str(datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")), 
                     'user': context.get('user'),
                     'version': version}
            filename = f"chatlogs-{session_id}.json"
            chat_log_full_path = os.path.join(chat_logs_path, filename)
            await save_chat_history(chat_log_full_path, chat_his_dict)
            print(f"Chat history saved to {filename}")
        return response.dict()

    async def ping(context=None):
        if login_required and context and context.get("user"):
            assert check_permission(context.get("user")), "You don't have permission to use the chatbot, please sign up and wait for approval"
        return "pong"

    channels = [collection['name'] for collection in collections]
    channels.append("learn")
    channels.append("function-call")
    hypha_service_info = await server.register_service({
        "name": "BioImage.IO Chatbot",
        "id": "bioimageio-chatbot",
        "config": {
            "visibility": "public",
            "require_context": True
        },
        "ping": ping,
        "chat": chat,
        "report": report,
        "channels": channels,
    })
    
    version = pkg_resources.get_distribution('bioimageio-chatbot').version
    
    with open(os.path.join(os.path.dirname(__file__), "static/index.html"), "r") as f:
        index_html = f.read()
    index_html = index_html.replace("https://ai.imjoy.io", server.config['public_base_url'] or f"http://127.0.0.1:{server.config['port']}")
    index_html = index_html.replace('"bioimageio-chatbot"', f'"{hypha_service_info["id"]}"')
    index_html = index_html.replace('v0.1.0', f'v{version}')
    index_html = index_html.replace("LOGIN_REQUIRED", "true" if login_required else "false")
    async def index(event, context=None):
        return {
            "status": 200,
            "headers": {'Content-Type': 'text/html'},
            "body": index_html
        }
    
    await server.register_service({
        "id": "bioimageio-chatbot-client",
        "type": "functions",
        "config": {
            "visibility": "public",
            "require_context": False
        },
        "index": index,
    })
    server_url = server.config['public_base_url']

    print(f"The BioImage.IO Chatbot is available at: {server_url}/{server.config['workspace']}/apps/bioimageio-chatbot-client/index")

if __name__ == "__main__":
    # asyncio.run(main())
    server_url = "https://ai.imjoy.io"
    loop = asyncio.get_event_loop()
    loop.create_task(connect_server(server_url))
    loop.run_forever()