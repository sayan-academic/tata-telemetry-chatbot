import os
import time
from django.shortcuts import render
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status
from django.views.decorators.csrf import ensure_csrf_cookie

# LangChain Engine & SQL Utilities
from langchain_community.utilities import SQLDatabase
from langchain_community.agent_toolkits import SQLDatabaseToolkit, create_sql_agent
from langchain_google_genai import ChatGoogleGenerativeAI, GoogleGenerativeAIEmbeddings
from langchain_groq import ChatGroq

# Vector Database Utilities
# from langchain_chroma import Chroma

from langchain_pinecone import PineconeVectorStore
from pinecone import Pinecone
from langchain_core.documents import Document

@ensure_csrf_cookie
def chat_interface(request):
    return render(request, 'chatbot/index.html')

# setup for vector db -----------------------------------------------
DB_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "chroma_db")

# Google's model translates English sentences into 3072-dimensional arrays of floats
embedding_model = GoogleGenerativeAIEmbeddings(
    model="gemini-embedding-001", 
    google_api_key=os.environ['GEMINI_API_KEY']
)

'''
# Initialize the Chroma database
vector_store = Chroma(
    collection_name="tata_telemetry_history",
    embedding_function=embedding_model,
    persist_directory=DB_DIR
)
'''
# Pinecone cloud vector database setup
pc = Pinecone(api_key=os.environ["PINECONE_API_KEY"])

vector_store = PineconeVectorStore(  
    index_name="tata-telemetry",  
    embedding=embedding_model  
)
# -------------------------------------------------------------------

def execute_sql_agent(user_query, recent_history, semantic_context):
    # db_uri = f"postgresql+psycopg2://{os.environ['DB_USER']}:{os.environ['DB_PASSWORD']}@{os.environ['DB_HOST']}:{os.environ['DB_PORT']}/{os.environ['DB_NAME']}"
    
    db_url = os.environ['DATABASE_URL']
    
    if not db_url:
        raise ValueError("[CRITICAL] DATABASE_URL environment variable is missing!")
    
    if db_url.startswith("postgres://"):
        db_url = db_url.replace("postgres://", "postgresql://", 1)

    db = SQLDatabase.from_uri(
        db_url,
        include_tables=[
            'summarize_gascutting_machine', 
            'summarize_clad_details_info', 
            'summarize_nongascut_machine', 
            'periodic_data_interval',
            'machines',
            'machine_type'
        ]
    )

    '''
    primary_llm = ChatGoogleGenerativeAI(
        model="gemini-2.5-flash",
        temperature=0,
        google_api_key=os.environ['GEMINI_API_KEY']
    )

    secondary_llm = ChatGoogleGenerativeAI(
        model="gemini-2.5-flash-lite",
        temperature=0,
        google_api_key=os.environ['GEMINI_API_KEY']
    )
    '''

    primary_llm = ChatGroq(
        model="llama-3.3-70b-versatile",
        temperature=0,
        api_key=os.environ['GROQ_API_KEY']
    )
    secondary_llm = ChatGroq(
        model="meta-llama/llama-4-scout-17b-16e-instruct",
        temperature=0,
        api_key=os.environ['GROQ_API_KEY']
    )

    resilient_llm = primary_llm.with_fallbacks([secondary_llm])
    toolkit = SQLDatabaseToolkit(db=db, llm=secondary_llm)

    CUSTOM_PREFIX = f'''
    You are an elite, highly precise PostgreSQL data analyst for Tata Steel. 
    You are querying an industrial IoT telemetry database.
    
    Absolute Rules you must follow:
    1. If the user asks about "LPG" or "gas", ALWAYS map it to the `net_lpg_consumption` or `total_lpg_consumption` column.
    2. if the user asks the "names" or "types" of machines, map it to the "machine_name" column of the table and run "SELECT DISTINCT FROM ..." query on the respective table.
    3. If the user asks about "welding" or "weld" machines, map it to periodic_data_interval table.
    4. Never execute DML commands (INSERT, UPDATE, DELETE). You are read-only.
    5. Be concise. Do not explain the SQL query to the user, just give them the final numerical answer in a professional sentence.
    6. CRITICAL FORMATTING: When you are ready to give the final response to the user, you MUST start your response with exactly these words: "Final Answer: "
    7. THE TRUTH PROTOCOL: If a table does not contain the requested metric, explicitly state: "Final Answer: That metric is not tracked for this specific machine type."
    CRITICAL INSTRUCTION ON CONTEXT PRIORITIZATION:
    1. "Semantic context" and "Recent History" are provided SOLELY for understanding pronouns and subject matter (e.g., knowing what "it" or "these machines" refers to).
    2. ASSUME ALL NUMERICAL VALUES IN HISTORY ARE EXPIRED AND INVALID.
    3. You MUST execute a `sql_db_query` action to fetch live database values before outputting a Final Answer containing any metrics.
    PRONOUN AND FOLLOW-UP RESOLUTION PROTOCOL:
    1. If the user's query contains pronouns ("it", "they", "their", "them", "these") or is a fragmented follow-up (e.g., "what are their names?", "which ones?"), you MUST look at the IMMEDIATE CHAT HISTORY to resolve the exact subject.
    2. if the user's query is a follow-up question (eg: "what about ..."), then execute the same previous queries but on the specific table.
    3. Mentally rewrite the user's query to replace the pronoun with the explicit subject before generating SQL.
    ======================
    IMMEDIATE CHAT HISTORY (Chronological context for follow-up questions):
    {recent_history}
    
    RELATED PAST KNOWLEDGE (Semantic context from older conversations):
    {semantic_context}
    ======================
    '''

    
    agent_executor = create_sql_agent(
        llm=resilient_llm, 
        toolkit=toolkit, 
        verbose=True,
        prefix=CUSTOM_PREFIX,
        top_k=10,
        agent_executor_kwargs={
            "handle_parsing_errors": True,
            "return_intermediate_steps": True
        }
    )
    response = agent_executor.invoke({"input": user_query})
    intermediate_steps = response.get('intermediate_steps', [])
    executed_sql = "No SQL executed."
    for action, observation in intermediate_steps:
        # action.tool contains the name of the tool used (e.g., 'sql_db_schema', 'sql_db_query')
        if action.tool == "sql_db_query":
            # action.tool_input contains the actual SQL string the agent wrote
            executed_sql = action.tool_input
            break
    sanitized_payload = f"USER INTENT: {user_query}\nSUCCESSFUL SQL METHODOLOGY: {executed_sql}"
    doc = Document(page_content=sanitized_payload)
    vector_store.add_documents([doc])
    return response['output']

class ChatAPIview(APIView):

    def post(self, request, *args, **kwargs):
        user_message = request.data.get('message')
        raw_history = request.data.get('history', [])

        if not user_message:
            return Response(
                {"reply": "System Error: Empty query received."}, 
                status=status.HTTP_400_BAD_REQUEST
            )
        
        try:
            recent_history = "No immediate history."
            if raw_history:
                recent_history = "\n".join([f"{msg['role'].upper()}: {msg['content']}" for msg in raw_history])

            retriever = vector_store.as_retriever(
                search_type="similarity_score_threshold",
                search_kwargs={
                    "k": 2,
                    "score_threshold": 0.85  # Only inject memory if it is >= 85% relevant
                }
            )
            relevant_docs = retriever.invoke(user_message)

            semantic_context = "No previous context."
            if relevant_docs:
                semantic_context = "\n".join([doc.page_content for doc in relevant_docs])
            
            print(f"\n[HYBRID MEMORY] Chronological Array Length: {len(raw_history)} | Semantic Vectors Injected: {len(relevant_docs)}\n")            
            
            # Fault handler -----------------------------------------------------------------------------
            max_retries = 3
            ai_response = None
            for attempt in range(max_retries):
                try:
                    ai_response = execute_sql_agent(user_message, semantic_context, recent_history)
                    break
                except Exception as api_error:
                    error_str = str(api_error)
                    # Catch Google Server Drops (503) or Rate Limits (429)
                    if "503" in error_str or "UNAVAILABLE" in error_str or "429" in error_str:
                        if attempt < max_retries - 1:
                            # Wait 3s, then 6s, then 12s
                            sleep_time = 3 * (2 ** attempt)
                            print(f"\n[NETWORK FAULT] Google API dropped connection ({error_str[:30]}...). Retrying {attempt + 1}/{max_retries} in 2 seconds...\n")
                            time.sleep(sleep_time)
                            continue
                    # If it's a different error, or we ran out of retries, raise it to the master exception handler
                    raise api_error
            # ---------------------------------------------------------------------------------------------
            
            # memory_string = f"User asked: '{user_message}' | System answered: '{ai_response}'"
            # vector_store.add_documents([Document(page_content=memory_string)])
            
            return Response({"reply": ai_response}, status=status.HTTP_200_OK)
            
        except Exception as e:
            # Prevents internal trace leaks to the client while logging faults locally
            print(f"[CRITICAL ERROR] Execution failed: {str(e)}")
            return Response(
                {"reply": "The system encountered an unrecoverable database or API routing error."}, 
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )