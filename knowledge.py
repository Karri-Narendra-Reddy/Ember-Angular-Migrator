import os
import logging
import requests
import httpx
import traceback

from dotenv import load_dotenv
from langchain_openai import AzureChatOpenAI
from openai import OpenAI

load_dotenv()

APIM_BASE_URL = os.getenv('APIM_BASE_URL', 'https://pre4apim.azure-api.net') 
APIM_KEY = os.getenv('APIM_KEY', '<API Key>')
APIM_VERSION = "2024-02-01"
O4_MINI_APIM_VERSION = "2024-12-01-preview"

# Azure OpenAI Deployments Avialable are
# gpt-4o-08
# gpt-4o-11
# gpt-4.1
# gpt-4o-mini
# gpt-4.1-mini
# gpt-4.1-nano
# gpt-5
# gpt-5-mini
# gpt-5-nano
# o4-mini
# text-embedding-3-large

endpoint = 'llmrouting-exp'

def call_using_responses_api(oai_deployment: str):
    # Define the API endpoint
    url = f"{APIM_BASE_URL}/openai/responses/"

    client = OpenAI(
        api_key=APIM_KEY,
        base_url=url,
        default_query={"api-version": os.getenv("API_VERSION")}
    )
    
    with client.responses.stream(
        model=oai_deployment,
        input="Who won the Super Bowl in 2017?"
    ) as stream:
        for event in stream:
            if event.type == "response.output_text.delta":
                print(event.delta, end="", flush=True)
        print()
    return ""
    

def call_apim_with_langchain(prompt_messages: list, oai_deployment: str, max_tokens: float=2000):
    logging.getLogger("httpx").setLevel(logging.WARNING)
    llm = AzureChatOpenAI(
        api_version=APIM_VERSION,
        api_key=APIM_KEY,
        azure_endpoint=APIM_BASE_URL + "/" + endpoint,
        azure_deployment=oai_deployment
    )
    response = llm.invoke(prompt_messages)
    return response.content
        
def call_apim_with_http_req(prompt_messages: list, oai_deployment: str, max_tokens: float=2000):
    # Define the API endpoint
    url = f"{APIM_BASE_URL}/{endpoint}/openai/deployments/{oai_deployment}/chat/completions?api-version={APIM_VERSION}"

    # Example with headers and parameters
    headers = {'api-key': APIM_KEY}
    payload = {'messages': prompt_messages}
    payload["max_tokens"] = max_tokens   
    
    response = requests.post(url, headers=headers,json=payload)
    if response.status_code == 200:
        data = response.json()
        llm_repsponse = data['choices'][0]['message']['content']
        return llm_repsponse
    else:
        print(f"Failed to retrieve data: {response.status_code} due to {response.text}")
        return None
        
def apim_for_o4_mini_with_http_req(prompt_messages: list, oai_deployment: str='o4-mini', max_completion_tokens: float=2000):
    # Define the API endpoint
    url = f"{APIM_BASE_URL}/{endpoint}/openai/deployments/{oai_deployment}/chat/completions?api-version={O4_MINI_APIM_VERSION}"

    # Example with headers and parameters
    headers = {'api-key': APIM_KEY}
    payload = {'messages': prompt_messages}
    payload["max_completion_tokens"] = max_completion_tokens   
    
    response = requests.post(url, headers=headers,json=payload)
    if response.status_code == 200:
        data = response.json()
        llm_repsponse = data['choices'][0]['message']['content']
        return llm_repsponse
    else:
        print(f"Failed to retrieve data: {response.status_code} due to {response.text}")
        return None
        
def o4_mini_with_langchain(prompt_messages: list, oai_deployment: str="o4-mini"):
    logging.getLogger("httpx").setLevel(logging.WARNING)
    llm = AzureChatOpenAI(
        api_version=O4_MINI_APIM_VERSION,
        api_key=APIM_KEY,
        azure_endpoint=APIM_BASE_URL + "/" + endpoint,
        azure_deployment=oai_deployment
    )
    response = llm.invoke(prompt_messages)
    return response.content

def call_apim_text_embedding(text_inputs:list, oai_deployment:str):
    url = f"{APIM_BASE_URL}/{endpoint}/openai/deployments/{oai_deployment}/embeddings?api-version={APIM_VERSION}"

    # Example with headers and parameters
    headers = {'api-key': APIM_KEY}
    payload = {'input':text_inputs, "user":"string", "input_type":"query"}  
    response = requests.post(url, headers=headers, json=payload)
    if response.status_code == 200:
        data = response.json()['data']
        embeddings = []
        for item in data:
            embeddings.append(item['embedding'])
        return embeddings
    else:
        print(f"Failed to retrieve embeddings: {response.status_code} due to {response.text}")
        return None
        
try:
    print("----------------Responses API---------------------------")
    llm_response_using_responses_api = call_using_responses_api("gpt-5")
    print(llm_response_using_responses_api)
    print("-----------------------------------------------------")
except Exception as e:
    traceback.print_exc()        

# Example Usage to call APIM with a HTTP Request
try:
    print("----------------GPT-4o-11---------------------------")
    llm_response_with_api = call_apim_with_http_req([{"role": "user", "content": "Who won the Super Bowl in 2017?"}], "gpt-4o-11")
    print(llm_response_with_api)
    print("-----------------------------------------------------")
except Exception as e:
    print(f"gpt-4o-11 is not configured in {endpoint}")

# Example Usage to call O4-Mini with langchain lib
try:
    print("-------------O4-mini with langchain-------------")
    o4_mini_langchain_response = o4_mini_with_langchain([{"role": "user", "content": "Who won the Super Bowl in 2017?"}], "o4-mini")
    print(o4_mini_langchain_response)
    print("-----------------------------------------------------")
except Exception as e:
    print(f"O4-mini is not configured in {endpoint}")

# Example Usage to call APIM with LangChain
try:
    print("----------------GPT-4.1---------------------------")
    llm_response_using_langchain = call_apim_with_langchain([{"role": "user", "content": "Who won the Super Bowl in 2017?"}], "gpt-4.1")
    print(llm_response_using_langchain)
    print("-----------------------------------------------------")
except Exception as e:
    print(f"gpt-4.1 is not configured in {endpoint}")
    
# Example Usage to call GPT-4.1-mini with LangChain
try:
    print("----------------GPT-4.1-mini---------------------------")
    gpt_41_mini = call_apim_with_langchain([{"role": "user", "content": "Who won the Super Bowl in 2017?"}], "gpt-4.1-mini")
    print(gpt_41_mini)
    print("-----------------------------------------------------")
except Exception as e:
    print(f"GPT-4.1-mini is not configured in {endpoint}")

# Example Usage to call GPT-4.1-nano with LangChain
try:
    print("----------------GPT-4.1-nano---------------------------")
    gpt_41_nano = call_apim_with_langchain([{"role": "user", "content": "Who won the Super Bowl in 2017?"}], "gpt-4.1-nano")
    print(gpt_41_nano)
    print("-----------------------------------------------------")
except Exception as e:
    print(f"GPT-4.1-nano is not configured in {endpoint}")
    
# Example Usage to call GPT-5 with LangChain
try:
    print("----------------GPT-5---------------------------")
    gpt_5 = call_apim_with_langchain([{"role": "user", "content": "Who won the Super Bowl in 2017?"}], "gpt-5")
    print(gpt_5)
    print("-----------------------------------------------------")
except Exception as e:
    print(e)
    
# Example Usage to call GPT-5-mini with LangChain
try:
    print("----------------GPT-5-mini---------------------------")
    gpt_5_mini = call_apim_with_langchain([{"role": "user", "content": "Who won the Super Bowl in 2017?"}], "gpt-5-mini")
    print(gpt_5_mini)
    print("-----------------------------------------------------")
except Exception as e:
    print(e)

# Example Usage to call GPT-5-mini with LangChain
try:
    print("----------------GPT-5-nano---------------------------")
    gpt_5_nano = call_apim_with_langchain([{"role": "user", "content": "Who won the Super Bowl in 2017?"}], "gpt-5-nano")
    print(gpt_5_nano)
    print("-----------------------------------------------------")
except Exception as e:
    print(e)     

# Example Usage of using o4-mini
try:
    print("----------------O4-mini with http----------------")
    o4_mini_response = apim_for_o4_mini_with_http_req([{"role": "user", "content": "Who won the Super Bowl in 2017?"}], "o4-mini")
    print(o4_mini_response)
    print("-----------------------------------------------------")
except Exception as e:
    print(f"O4-mini with http is not configured in {endpoint}")

# Example Usage to call APIM for Text Embeddings
try:
    print("-------------text-embedding-3-large------------------")
    embeddings = call_apim_text_embedding([llm_response_using_langchain], "text-embedding-3-large")
    print(f"Got text embeddings of length : {len(embeddings[0])}")
    print("-----------------------------------------------------")
except Exception as e:
    print(f"text-embedding-3-large is not configured in {endpoint}")
