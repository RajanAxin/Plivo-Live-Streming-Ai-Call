import plivo
from quart import Quart, websocket, Response, request
import asyncio
import websockets
import datetime
import pytz
import requests
import json
import logging
import base64
from dotenv import load_dotenv
import os
from urllib.parse import quote, urlencode
from database import get_db_connection
import concurrent.futures
import datetime
import aiohttp
import re
import html
import time

load_dotenv(dotenv_path='.env', override=True)
OPENAI_API_KEY = os.getenv('OPENAI_API_KEY')
PLIVO_AUTH_ID = os.getenv('PLIVO_AUTH_ID')
PLIVO_AUTH_TOKEN = os.getenv('PLIVO_AUTH_TOKEN')

if not OPENAI_API_KEY:
    raise ValueError("OPENAI_API_KEY environment variable is not set. Please add it to your .env file")

PORT = 5000

# Updated SYSTEM_MESSAGE with clearer instructions
SYSTEM_MESSAGE = (
    "CRITICAL RULE: After completing a FULL question, STOP speaking and WAIT for the user's response. "
    "Always complete your sentences and thoughts before stopping. "
    "Do not stop in the middle of a sentence or phrase. "
    "Always speak briefly (1-2 sentences). Ask one question, then wait for the user's response. "
    "NEVER continue talking, NEVER add more context, and NEVER ask a follow-up until the user replies. "
    "INTRODUCTION RULE: At the start of the call, ONLY say: 'Hi, this is <agent_name>. Am I speaking to <lead_name>?' "
    "This introduction MUST be the ONLY thing spoken. Do not add any other words. Do not continue with any sales pitch. Do not say 'Great' or anything else. "
    "If the user answers, then continue the conversation based on their response. "
    "WAIT FOR USER CONFIRMATION: After asking 'Am I speaking to [name]?', you MUST wait for the user to confirm before proceeding. Do NOT continue with questions about moving services until the user confirms their identity. "
    "If the user confirms their identity (e.g., 'Yes', 'That's me', 'Speaking'), then ask: 'Great! How are you today?' and wait for response. "
    "Only after they respond to 'How are you?' should you proceed with moving service questions. "
    "When asking how the user is doing, ONLY say exactly one of: 'How are you?' or 'Hi <lead_name>. How are you?'. Then STOP. "
    "Do not repeat the same response back-to-back. "
    "If the user says sorry then please repeat your last question. "
    "If the user says 'okay' or 'ok' then please ask next question. "
    "If the user says 'No I'm <name>' or 'No this is <name>' then respond with: 'Sorry about that <name>. How are you?' "
    
    "If the user says 'invalid number', 'wrong number', 'already booked', or 'I booked with someone else', respond with: 'No worries, sorry to bother you. Have a great day.' "
    "If the user says 'don't call', 'do not call', 'not to call', 'not interested', 'not looking', 'take me off', 'unsubscribe', or 'remove me from your list', respond with: 'No worries, sorry to bother you. Have a great day.' "
    "If the user asks about 'truck rental', 'van rental', or 'truck rent', respond with: 'We provide moving services, sorry to bother you. Have a great day.' "
    "If the user says 'bye', 'goodbye', 'take care', or 'see you', respond with: 'Nice to talk with you. Have a great day.' "
    "If the user says 'busy', 'call me later', 'not available', 'in a meeting', 'occupied', 'voicemail', or anything meaning they cannot talk now, respond with: 'I will call you later. Nice to talk with you. Have a great day.' "
    "If silence is detected, only respond with: 'Are you there?'. Do not say anything else."
)

app = Quart(__name__)

# Initialize database table
def initialize_database():
    conn = get_db_connection()
    if conn:
        try:
            cursor = conn.cursor()
            # Create conversation_messages table if it doesn't exist
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS conversation_messages (
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    lead_id INT NOT NULL DEFAULT '0',
                    conversation_id VARCHAR(255) NOT NULL,
                    speaker ENUM('system', 'user', 'assistant') NOT NULL,
                    content TEXT NOT NULL,
                    timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
                    INDEX (conversation_id)
                )
            """)
            conn.commit()
            print("Database initialized successfully")
        except Exception as e:
            print(f"Database initialization error: {e}")
        finally:
            if conn.is_connected():
                cursor.close()
                conn.close()
    else:
        print("Failed to get database connection for initialization")

# Function to log conversation to database
def log_conversation_to_db(lead_id, conversation_id, speaker, content):
    conn = get_db_connection()
    if conn:
        try:
            cursor = conn.cursor()
            query = """
                INSERT INTO conversation_messages (lead_id, conversation_id, speaker, content)
                VALUES (%s, %s, %s, %s)
            """
            cursor.execute(query, (lead_id, conversation_id, speaker, content))
            conn.commit()
            print(f"[DB] Logged {speaker} message for conversation {conversation_id}")
        except Exception as e:
            print(f"Database error: {e}")
        finally:
            if conn.is_connected():
                cursor.close()
                conn.close()
    else:
        print("Failed to get database connection")

# Async wrapper for database logging
async def log_conversation(lead_id, conversation_id, speaker, content):
    loop = asyncio.get_event_loop()
    with concurrent.futures.ThreadPoolExecutor() as pool:
        await loop.run_in_executor(
            pool, 
            log_conversation_to_db, 
            lead_id, conversation_id, speaker, content
        )

# Function to hang up call using Plivo API
async def hangup_call(call_uuid, disposition, lead_id, text_message="I have text", followup_datetime=None):
    if not PLIVO_AUTH_ID or not PLIVO_AUTH_TOKEN:
        print("Plivo credentials not set. Cannot hang up call.")
        return
    
    # 1️⃣ Hang up the call via Plivo API
    url = f"https://api.plivo.com/v1/Account/{PLIVO_AUTH_ID}/Call/{call_uuid}/"
    auth_string = f"{PLIVO_AUTH_ID}:{PLIVO_AUTH_TOKEN}"
    auth_header = base64.b64encode(auth_string.encode()).decode()
    print(f"[DEBUG] Attempting to hang up call {call_uuid}")
    print(f"[DEBUG] URL: {url}")
    print(f"[DEBUG] Auth header: {auth_header[:5]}...")
    
    try:
        async with aiohttp.ClientSession() as session:
            async with session.delete(
                url,
                headers={"Authorization": f"Basic {auth_header}"}
            ) as resp:
                print(f"[DEBUG] Response status: {resp.status}")
                if resp.status == 204:
                    print(f"Successfully hung up call {call_uuid}")
                else:
                    response_text = await resp.text()
                    print(f"Failed to hang up call {call_uuid}: {resp.status} {response_text}")
    except Exception as e:
        print(f"Error hanging up call: {e}")
    
    # 2️⃣ Build query params for Redirect
    params = {
        "lead_id": lead_id,
        "disposition": disposition
    }
    if followup_datetime:
        params["followupdatetime"] = followup_datetime
        print(f"[DEBUG] Including followupdatetime: {followup_datetime}")

    # Build the URL with proper encoding
    query_string = urlencode(params, quote_via=quote)
    redirect_url = f"http://54.176.128.91/disposition_route?{query_string}"
    response = requests.post(redirect_url)
    print(f"[DEBUG] Redirect URL: {response}")

def get_timezone_from_db(timezone_id: int):
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    cursor.execute("SELECT timezone FROM mst_timezone WHERE timezone_id = %s LIMIT 1", (timezone_id,))
    row = cursor.fetchone()
    cursor.close()
    conn.close()
    if row:
        return row["timezone"]   # e.g., "Asia/Kolkata"
    return None

# Function to check disposition based on user input
def check_disposition(transcript, lead_timezone):
    transcript_lower = transcript.lower()
    
    # Pattern 1: Do not call
    if re.search(r"\b(don'?t call|do not call|not to call|take me off)\b", transcript_lower):
        return 2, "No worries, sorry to bother you. Have a great day", None
    
    # Pattern 2: Wrong number
    elif re.search(r"\b(wrong number|invalid number|incorrect number)\b", transcript_lower):
        return 7, "No worries, sorry to bother you. Have a great day", None
    
    # Pattern 3: Not interested
    elif re.search(r"\b(not looking to move|not looking|not interested)\b", transcript_lower):
        return 3, "No worries, sorry to bother you. Have a great day", None
    
    # Pattern 4: Not available
    elif re.search(r"\b(not available|hang up or press|reached the maximum time allowed to make your recording|at the tone|record your message|voicemail|voice mail|leave your message|are busy|am busy|busy|call me later|call me|call me at)\b", transcript_lower):
        # Check if it's a busy/call me later pattern that might have a datetime
        if re.search(r"\b(are busy|am busy|busy|call me later|call me|call me at)\b", transcript_lower):
            followup_datetime = get_followup_datetime(transcript_lower, lead_timezone)
            print(f'🎤 Lead Timezone: {lead_timezone}')
            print(f'🎤 Followup DateTime: {followup_datetime}')
            if followup_datetime:
                return 4, "I will call you later. Nice to talk with you. Have a great day.", followup_datetime
        # Default response for voicemail or no datetime found
        return 6, "I will call you later. Nice to talk with you. Have a great day.", None
    
    # Pattern 5: Truck rental
    #elif re.search(r"\b(truck rental|looking for truck rent|truck rent|van rental|van rent)\b", transcript_lower):
    #    return 13, "We are providing moving services, sorry to bother you. Have a great day", None
    
    # Pattern 6: Already booked
    elif re.search(r"\b(already booked|booked)\b", transcript_lower):
        return 8, "No worries, sorry to bother you. Have a great day", None
    
    # Pattern 7: Goodbye
    elif re.search(r"\b(bye|goodbye|good bye|take care|see you)\b", transcript_lower):
        return 6, "Nice to talk with you. Have a great day", None
    
    # Default disposition
    return 6, None, None

def get_followup_datetime(user_speech, timezone_id: int):
    # Fetch timezone from DB
    tz_name = get_timezone_from_db(timezone_id)
    if not tz_name:
        logging.error(f"No timezone found for ID: {timezone_id}")
        return None
    tz = pytz.timezone(tz_name)
    current_datetime = datetime.datetime.now(tz).strftime("%m/%d/%Y %I:%M %p")
    print(f"Speech: {user_speech} -- {current_datetime}")
    logging.info(f"Speech: {user_speech} -- {current_datetime}")
    system_prompt = f"""
    You are a date/time parser. Extract exact datetime from user message and return valid JSON like: {{"datetime": "MM/DD/YYYY HH:MM AM/PM"}}.
    If no datetime, use null.
    Instructions:
    - Assume today date and time is: {current_datetime}
    - If the user says "after half an hour","tomorrow evening","after 15 minutes","next month", "after 15th", "next week", etc., calculate based on this current date.
    - Do NOT return any year earlier than 2025.
    - If no time is mentioned, use current time from today: {current_datetime}
    - If user says "evening", use 06:00 PM. If "morning", use 09:00 AM. If "afternoon", use 01:00 PM. If "night", use 08:00 PM.
    """
    try:
        response = requests.post(
            "https://api.openai.com/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {OPENAI_API_KEY}",
                "Content-Type": "application/json"
            },
            json={
                "model": "gpt-4o",
                "temperature": 0.2,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_speech}
                ]
            },
            timeout=60
        )
        if response.status_code == 200:
            gpt_content = response.json()["choices"][0]["message"]["content"]
            data = json.loads(gpt_content)
            print(f'🎤 Followup Data: {data}')
            return data.get("datetime")
        else:
            logging.error(f"OpenAI API failed: {response.text}")
            return None
    except Exception as e:
        logging.error(f"Followup datetime error: {e}")
        return None

# New function to create response with completion instructions
async def create_response_with_completion_instructions(openai_ws, instructions, temperature=0.7):
    """Create a response with explicit instructions to complete sentences"""
    response_create = {
        "type": "response.create",
        "response": {
            "modalities": ["text", "audio"],
            "temperature": temperature,
            "instructions": f"{instructions}\n\nIMPORTANT: Always complete your sentences and thoughts. Never stop speaking in the middle of a sentence or phrase."
        }
    }
    await openai_ws.send(json.dumps(response_create))

# Updated send_Session_update function
async def send_Session_update(openai_ws, prompt_text, voice_name):
    # Combine the system message with the custom prompt
    full_prompt = f"{SYSTEM_MESSAGE}\n\n{prompt_text}\n\nIMPORTANT: Always complete your sentences and thoughts. Never stop speaking in the middle of a sentence or phrase."
    
    session_update = {
        "type": "session.update",
        "session": {
            "turn_detection": {"type": "server_vad"},
            "tools": [],
            "input_audio_format": "g711_ulaw",
            "output_audio_format": "g711_ulaw",
            "voice": voice_name,
            "instructions": full_prompt,
            "modalities": ["text", "audio"],
            "temperature": 0.7,  # Lowered temperature for more consistent responses
            "input_audio_transcription": {"model": "whisper-1", "language": "en"} 
        }
    }
    await openai_ws.send(json.dumps(session_update))

def function_call_output(arg, item_id, call_id):
    sum_val = int(arg['num1']) + int(arg['num2'])
    conversation_item = {
        "type": "conversation.item.create",
        "item": {
            "id": item_id,
            "type": "function_call_output",
            "call_id": call_id,
            "output": str(sum_val)
        }
    }
    return conversation_item

@app.route("/answer", methods=["GET", "POST"])
async def home():
    # Extract the caller's number (From) and your Plivo number (To)
    from_number = (await request.form).get('From') or request.args.get('From')
    from_number = from_number[1:] if from_number else None
    to_number = (await request.form).get('To') or request.args.get('To')
    call_uuid = (await request.form).get('CallUUID') or request.args.get('CallUUID')
    
    print(f"Inbound call from: {from_number} to: {to_number} (Call UUID: {call_uuid})")
    
    # Default values
    brand_id = 1
    voice_name = 'alloy'
    voice_id = 'CwhRBWXzGAHq8TQ4Fs17'
    audio = 'plivoai/vanline_inbound.mp3'
    audio_message = "HI, This is ai-agent. Tell me what can i help you?"
    ai_agent_id = None  # Initialize ai_agent_id
    
    # Database queries using mysql.connector
    conn = get_db_connection()
    if conn:
        try:
            cursor = conn.cursor(dictionary=True)
            
            # Query lead data
            cursor.execute("""
                SELECT * FROM leads 
                WHERE phone = %s 
                ORDER BY lead_id DESC 
                LIMIT 1
            """, (from_number,))
            lead_data = cursor.fetchone()
            
            # Query call number
            cursor.execute("SELECT * FROM call_number WHERE number = %s", (to_number,))
            call_number = cursor.fetchone()
            
            if call_number:
                brand_id = call_number['brand_id']
                
                # Query brand voice
                cursor.execute("SELECT * FROM brand_voice WHERE brand_id = %s", (brand_id,))
                brand_voice = cursor.fetchone()
                
                cursor.execute("SELECT * FROM ai_agents WHERE brand_id = %s and agent_status = 'active'", (brand_id,))
                ai_agent = cursor.fetchone()
                
                if ai_agent:
                    ai_agent_id = ai_agent['id']  # Get the ai_agent_id
                    # Note: We're no longer fetching the prompt here
                    
                if brand_voice:
                    cursor.execute("SELECT * FROM mst_voiceid WHERE voice_id = %s", (brand_voice['voice_id'],))
                    voice = cursor.fetchone()
                    if voice:
                        voice_name = voice['voice_name']
                        if voice['voice_prompt_id']:
                            voice_id = voice['voice_prompt_id']
                
                # Audio selection logic
                if brand_id == 1:
                    audio = 'plivoai/vanline_inbound.mp3'
                    if lead_data and lead_data['type'] == "outbound":
                        audio = 'plivoai/vanline_intro.mp3'
                elif brand_id == 2:
                    audio = 'plivoai/interstates_inbound.mp3'
                    if lead_data and lead_data['type'] == "outbound":
                        audio_message = f"HI, This is {ai_agent['agent_name'] if ai_agent else 'AI Agent'}. Am I speaking to {lead_data['name']}?"
                        audio = "plivoai/customer1.mp3"
                        
        except Exception as e:
            print(f"Database query error: {e}")
        finally:
            if conn.is_connected():
                cursor.close()
                conn.close()
    
    lead_id = lead_data['lead_id'] if lead_data else 0
    lead_timezone = lead_data['t_timezone'] if lead_data else 0
    print(f"agent_id: {ai_agent_id if ai_agent_id else 'N/A'}")
    
    # Note: We're no longer storing the prompt in a global dictionary
    
    # print(f"lead_id: {lead_id}")
    
    ws_url = (
    f"wss://{request.host}/media-stream?"
    f"audio_message={quote(audio_message)}"
    f"&amp;CallUUID={call_uuid}"
    f"&amp;From={from_number}"
    f"&amp;To={to_number}"
    f"&amp;lead_id={lead_id}"
    f"&amp;voice_name={voice_name}"
    f"&amp;ai_agent_id={ai_agent_id}"  # Add ai_agent_id to the URL
    f"&amp;lead_timezone={lead_timezone}"  # Add lead_timezone to the URL
    )              
    
    # XML response
    xml_data = f'''<?xml version="1.0"?>
    <Response>
        <Stream streamTimeout="86400" keepCallAlive="true" bidirectional="true" 
                contentType="audio/x-mulaw;rate=8000" audioTrack="inbound">
            {ws_url}
        </Stream>
    </Response>'''
    
    return Response(xml_data, mimetype='application/xml')

@app.route("/test-disposition", methods=["GET", "POST"])
async def test_disposition():
    # Sample parameters for testing
    lead_id = request.args.get('lead_id', '31')
    disposition = request.args.get('disposition', '4')
    followup_datetime = request.args.get('followup_datetime', '09/04/2025 08:15 PM')
    text_message = "I will call you later. Nice to talk with you. Have a great day."
    
    # Build query params for Redirect
    params = {
        "lead_id": lead_id,
        "disposition": disposition
    }
    if followup_datetime:
        params['followupdatetime'] = followup_datetime
    
    # Build the URL with proper encoding
    query_string = urlencode(params, quote_via=quote)
    redirect_url = f"http://54.176.128.91/disposition_route?{query_string}"
    
    # Build XML Response
    content = f'''<?xml version="1.0" encoding="UTF-8"?>
            <Response>
                <Play>{text_message}</Play>
                <Redirect method="POST">{redirect_url}</Redirect>
            </Response>'''
    
    # Return XML response
    return Response(content, status=200, mimetype="text/xml")

@app.route("/test", methods=["GET", "POST"])
async def test():
    print("Test endpoint hit!")
    print("Form data:", await request.form)
    return Response("Test successful", mimetype='text/plain')

@app.websocket('/media-stream')
async def handle_message():
    print('Client connected')
    plivo_ws = websocket 
    audio_message = websocket.args.get('audio_message', "Hi this is verse How can i help you?")
    call_uuid = websocket.args.get('CallUUID', 'unknown')
    voice_name = websocket.args.get('voice_name', 'alloy')
    ai_agent_id = websocket.args.get('ai_agent_id')  # Get ai_agent_id from URL params
    lead_id = websocket.args.get('lead_id', 'unknown')
    lead_timezone = websocket.args.get('lead_timezone', 'unknown')
    
    print('audio_message', audio_message)
    print('voice_name', voice_name)
    print('ai_agent_id', ai_agent_id)
    print('lead_timezone', lead_timezone)
    
    # Initialize conversation state
    conversation_state = {
        'in_ai_response': False,
        'current_ai_text': '',
        'conversation_id': call_uuid,
        'from_number': websocket.args.get('From', ''),
        'to_number': websocket.args.get('To', ''),
        'lead_timezone': lead_timezone,
        'lead_id': lead_id,
        'active_response': False,  # Track if there's an active response to cancel
        'response_items': {},  # Store response items by ID
        'pending_language_reminder': False,  # Flag to send language reminder after current response
        'ai_transcript': '',  # Accumulate AI transcript
        'disposition': 6,  # Default disposition
        'pending_hangup': False,  # Flag to hang up after current response
        'disposition_message': None,  # Store disposition message
        'is_disposition_response': False,  # Track if current response is a disposition response
        'disposition_response_id': None,  # Store the ID of the disposition response
        'disposition_audio_sent': False,  # Track if disposition audio has been sent
        # Audio buffering for smoother playback
        'audio_buffer': b'',  # Buffer to collect audio chunks
        'audio_buffer_timer': None,  # Timer to flush buffer periodically
        'total_audio_bytes': 0,  # Track total audio sent for timing calculation
        'audio_start_time': None,  # When audio started playing
        # Timeout system
        'timeout_task': None,  # Active timeout timer
        'waiting_for_user': False,  # Flag indicating we're waiting for user response
        'are_you_there_count': 0,  # Count of "Are you there?" attempts
        'max_are_you_there': 3,  # Maximum attempts before hanging up
        'user_speaking': False,  # Track if user is currently speaking
        'is_are_you_there_response': False,  # Track if current response is "Are you there?"
        'call_ending': False,  # Flag to indicate call is ending
        'disposition_hangup_scheduled': False,  # NEW: Flag to prevent multiple hangup attempts
    }
    
    # Fetch prompt_text from database using ai_agent_id
    prompt_text = SYSTEM_MESSAGE  # Default to system message
    if ai_agent_id:
        conn = get_db_connection()
        if conn:
            try:
                cursor = conn.cursor(dictionary=True)
                # Fetch the active prompt for this ai_agent
                cursor.execute("SELECT * FROM ai_agent_prompts WHERE ai_agent_id = %s and is_active = 1", (ai_agent_id,))
                ai_agent_prompt = cursor.fetchone()
                if ai_agent_prompt:
                    prompt_text = ai_agent_prompt['prompt_text']
                    # If we have lead_id, fetch lead data and replace placeholders
                    if lead_id and lead_id != 'unknown':
                        try:
                            lead_id_int = int(lead_id)
                            cursor.execute("SELECT * FROM leads WHERE lead_id = %s", (lead_id_int,))
                            lead_data = cursor.fetchone()
                            if lead_data:
                                for key, value in lead_data.items():
                                    placeholder = f"[lead_{key}]"
                                    prompt_text = prompt_text.replace(placeholder, str(value))
                        except (ValueError, TypeError):
                            print(f"Invalid lead_id: {lead_id}")
            except Exception as e:
                print(f"Error fetching prompt in handle_message: {e}")
            finally:
                if conn.is_connected():
                    cursor.close()
                    conn.close()
    
    # Combine with SYSTEM_MESSAGE
    prompt_to_use = f"{SYSTEM_MESSAGE}\n\n{prompt_text}"
    print(f"prompt_text: {prompt_to_use}")
    
    # Timeout handler functions
    async def handle_timeout():
        """Handle 5-second timeout by sending 'Are you there?' message"""
        try:
            # Check if we're already in a timeout state to prevent multiple triggers
            if conversation_state.get('in_timeout_handling', False):
                return
                
            conversation_state['in_timeout_handling'] = True
            conversation_state['are_you_there_count'] += 1
            
            import time
            current_time = time.strftime("%H:%M:%S", time.localtime())
            print(f"[TIMEOUT] 5 seconds elapsed at {current_time} without user response, sending 'Are you there?' (attempt {conversation_state['are_you_there_count']}/{conversation_state['max_are_you_there']})")
            
            # Reset timeout state
            conversation_state['timeout_task'] = None
            conversation_state['waiting_for_user'] = False
            # Reset conversation state to ensure a clean response
            conversation_state['in_ai_response'] = False
            conversation_state['current_ai_text'] = ''
            
            # Check if we've reached the maximum attempts
            if conversation_state['are_you_there_count'] >= conversation_state['max_are_you_there']:
                print(f"[TIMEOUT] Reached maximum 'Are you there?' attempts ({conversation_state['max_are_you_there']}), disconnecting call")
                # Set disposition for no response and prepare to hang up
                conversation_state['disposition'] = 6  # Default disposition for no response
                conversation_state['disposition_message'] = "Thank you for your time. Have a great day."
                # Create final goodbye message before hanging up
                goodbye_response = {
                    "type": "response.create",
                    "response": {
                        "modalities": ["text", "audio"],
                        "temperature": 0.6,  # Set to minimum allowed temperature
                        "instructions": "Say exactly: 'Thank you for your time. Have a great day.' and then end the conversation."
                    }
                }
                await openai_ws.send(json.dumps(goodbye_response))
                conversation_state['active_response'] = True
                conversation_state['is_disposition_response'] = True
                conversation_state['in_timeout_handling'] = False
                return
            
            # First, cancel any active response to ensure a clean state
            if conversation_state.get('active_response', False):
                cancel_response = {
                    "type": "response.cancel"
                }
                await openai_ws.send(json.dumps(cancel_response))
                conversation_state['active_response'] = False
                # Small delay to ensure cancellation is processed
                await asyncio.sleep(0.2)
            
            # Create a conversation item that sets the context for the timeout
            context_item = {
                "type": "conversation.item.create",
                "item": {
                    "type": "message",
                    "role": "system",
                    "content": [
                        {
                            "type": "input_text",
                            "text": "CRITICAL TIMEOUT INSTRUCTION: The user has not responded for 5 seconds. You MUST say ONLY the words 'Are you there?' and then stop. Do not add any other words, do not continue the conversation, and do not say anything else. This instruction overrides all previous instructions."
                        }
                    ]
                }
            }
            await openai_ws.send(json.dumps(context_item))
            # Small delay to ensure the context item is processed
            await asyncio.sleep(0.2)
        
            # Create timeout response for "Are you there?"
            timeout_response = {
                "type": "response.create",
                "response": {
                    "modalities": ["text", "audio"],
                    "temperature": 0.6,  # Set to minimum allowed temperature
                    "instructions": "Say ONLY these exact words: 'Are you there?' Do not add any other words. Do not continue the conversation. After saying this, stop and wait for the user's response."
                }
            }
            await openai_ws.send(json.dumps(timeout_response))
            conversation_state['active_response'] = True
            conversation_state['is_are_you_there_response'] = True
            conversation_state['in_timeout_handling'] = False
        except Exception as e:
            print(f"[TIMEOUT] Error handling timeout: {e}")
            # Ensure the timeout handling flag is reset even on error
            conversation_state['in_timeout_handling'] = False

    def start_timeout_timer():
        """Start a 5-second timer for user response"""
        # Cancel existing timer if any
        if conversation_state['timeout_task'] and not conversation_state['timeout_task'].done():
            print("[TIMEOUT] Cancelling existing timeout task")
            conversation_state['timeout_task'].cancel()
        # Only start timer if not in a disposition flow and not already waiting
        if (not conversation_state.get('is_disposition_response', False) and
            not conversation_state.get('pending_hangup', False) and
            not conversation_state.get('waiting_for_user', False) and
            not conversation_state.get('call_ending', False)):  # Check if call is ending
            print("[TIMEOUT] ⏰ Starting 5-second timeout timer")
            conversation_state['waiting_for_user'] = True
            conversation_state['timeout_task'] = asyncio.create_task(asyncio.sleep(5))
            # Schedule the timeout handler
            async def timeout_wrapper():
                try:
                    print("[TIMEOUT] ⏱️ Starting 5-second countdown...")
                    await conversation_state['timeout_task']
                    if conversation_state.get('waiting_for_user', False):
                        print("[TIMEOUT] ⏰ 5 seconds elapsed! Calling handle_timeout()")
                        await handle_timeout()
                    else:
                        print("[TIMEOUT] ⏰ Timer completed but waiting_for_user=False, not calling handle_timeout")
                except asyncio.CancelledError:
                    print("[TIMEOUT] ❌ Timer cancelled")
                except Exception as e:
                    print(f"[TIMEOUT] ❌ Timer error: {e}")
            asyncio.create_task(timeout_wrapper())
        else:
            print(f"[TIMEOUT] ❌ Timer conditions not met - not starting timer")

    def cancel_timeout_timer():
        """Cancel the timeout timer"""
        if conversation_state['timeout_task'] and not conversation_state['timeout_task'].done():
            conversation_state['timeout_task'].cancel()
            print("[TIMEOUT] ❌ Timer cancelled - user activity detected")
        else:
            print("[TIMEOUT] ❌ Timer cancel requested but no active timer")
        conversation_state['waiting_for_user'] = False
        conversation_state['timeout_task'] = None
    
    url = "wss://api.openai.com/v1/realtime?model=gpt-4o-realtime-preview-2024-10-01"
    headers = {
        "Authorization": f"Bearer {OPENAI_API_KEY}",
        "OpenAI-Beta": "realtime=v1",
    }
    
    try: 
        async with websockets.connect(url, extra_headers=headers) as openai_ws:
            print('Connected to the OpenAI Realtime API')
            
            # Send session update first
            await send_Session_update(openai_ws, prompt_to_use, voice_name)
            await asyncio.sleep(0.5)
            
            # Send the specific audio_message as initial prompt using the new function
            initial_prompt = (
                f"Start with this exact phrase: '{audio_message}' "
                f"Wait for the user to confirm their identity. "
                f"If they confirm (say 'Yes', 'That's me', or 'Speaking'), then ask: 'Great! How are you today?' and wait for response. "
                f"If they don't confirm but give their name, respond with: 'Sorry about that [name]. How are you today?'"
                f"For this initial introduction only, follow these instructions instead of the WAIT FOR USER CONFIRMATION rule."
                f"IMPORTANT: Always complete your sentences and thoughts. Never stop speaking in the middle of a sentence or phrase."
            )
            
            await create_response_with_completion_instructions(openai_ws, initial_prompt)
            conversation_state['active_response'] = True  # Mark that we have an active response
            
            receive_task = asyncio.create_task(receive_from_plivo(plivo_ws, openai_ws))
            
            async for message in openai_ws:
                await receive_from_openai(message, plivo_ws, openai_ws, conversation_state, handle_timeout)
            
            await receive_task
    
    except asyncio.CancelledError:
        print('Client disconnected')
    except websockets.ConnectionClosed:
        print("Connection closed by OpenAI server")
    except Exception as e:
        print(f"Error during OpenAI's websocket communication: {e}")

async def receive_from_plivo(plivo_ws, openai_ws):
    try:
        while True:
            message = await plivo_ws.receive()
            data = json.loads(message)
            if data['event'] == 'media' and openai_ws.open:
                audio_append = {
                    "type": "input_audio_buffer.append",
                    "audio": data['media']['payload']
                }
                await openai_ws.send(json.dumps(audio_append))
            elif data['event'] == "start":
                print('Plivo Audio stream has started')
                plivo_ws.stream_id = data['start']['streamId']
    except websockets.ConnectionClosed:
        print('Connection closed for the plivo audio streaming servers')
        if openai_ws.open:
            await openai_ws.close()
    except Exception as e:
        print(f"Error during Plivo's websocket communication: {e}")

async def receive_from_openai(message, plivo_ws, openai_ws, conversation_state, handle_timeout):
    try:
        response = json.loads(message)
        event_type = response.get('type', 'unknown')
        
        # Log all event types for debugging (except audio deltas to reduce noise)
        if event_type not in ['response.audio.delta', 'response.audio.done']:
            print(f"[DEBUG] Received event: {event_type}")
        
        # Handle AI text responses
        if event_type == 'response.text.delta':
            # If this is a disposition response, handle it specially
            if conversation_state.get('is_disposition_response', False):
                delta = response.get('delta', '')
                print(f"[AI Disposition] {delta}", end='', flush=True)
                conversation_state['disposition_text'] = conversation_state.get('disposition_text', '') + delta
                return
                
            delta = response.get('delta', '')
            item_id = response.get('item_id', '')
            
            if not conversation_state['in_ai_response']:
                print(f"[AI] {delta}", end='', flush=True)
                conversation_state['in_ai_response'] = True
                conversation_state['current_ai_text'] = delta
                
                # Initialize this response item if not exists
                if item_id and item_id not in conversation_state['response_items']:
                    conversation_state['response_items'][item_id] = delta
                elif item_id:
                    conversation_state['response_items'][item_id] += delta
            else:
                print(delta, end='', flush=True)
                conversation_state['current_ai_text'] += delta
                if item_id:
                    conversation_state['response_items'][item_id] += delta
                
        elif event_type == 'response.text.done':
            # If this is a disposition response, handle it specially
            if conversation_state.get('is_disposition_response', False):
                text = response.get('text', '') or conversation_state.get('disposition_text', '')
                print()  # Newline after AI response
                print(f"[LOG] AI Disposition Response: {text}")
                # Log to database
                await log_conversation(
                    conversation_state['lead_id'],
                    conversation_state['conversation_id'],
                    'assistant',
                    text
                )
                
                # Don't reset disposition tracking yet
                return
                
            item_id = response.get('item_id', '')
            text = response.get('text', '')
            
            print()  # Newline after AI response
            conversation_state['in_ai_response'] = False
            
            # Use the text from the event if available, otherwise use accumulated text
            if text:
                print(f"[LOG] AI Text Response: {text}")
                # Check if the response ends with a sentence terminator
                if not any(text.endswith(punct) for punct in ['.', '!', '?']):
                    print(f"[WARNING] AI response may be incomplete: {text}")
                    # If the response seems incomplete, create a new response to complete it
                    if not conversation_state.get('is_disposition_response', False):
                        completion_prompt = f"Please complete your previous thought: '{text}'"
                        await create_response_with_completion_instructions(openai_ws, completion_prompt)
                        conversation_state['active_response'] = True
                
                # Log to database
                await log_conversation(
                    conversation_state['lead_id'],
                    conversation_state['conversation_id'],
                    'assistant',
                    text
                )
            else:
                print(f"[LOG] AI Response: {conversation_state['current_ai_text']}")
                # Check if the response ends with a sentence terminator
                if not any(conversation_state['current_ai_text'].endswith(punct) for punct in ['.', '!', '?']):
                    print(f"[WARNING] AI response may be incomplete: {conversation_state['current_ai_text']}")
                    # If the response seems incomplete, create a new response to complete it
                    if not conversation_state.get('is_disposition_response', False):
                        completion_prompt = f"Please complete your previous thought: '{conversation_state['current_ai_text']}'"
                        await create_response_with_completion_instructions(openai_ws, completion_prompt)
                        conversation_state['active_response'] = True
                
                # Log to database
                await log_conversation(
                    conversation_state['lead_id'],
                    conversation_state['conversation_id'],
                    'assistant',
                    conversation_state['current_ai_text']
                )
                
            conversation_state['current_ai_text'] = ''
            
            # Remove this item from tracking
            if item_id and item_id in conversation_state['response_items']:
                del conversation_state['response_items'][item_id]
                
        # Handle AI audio transcript
        elif event_type == 'response.audio_transcript.delta':
            # If this is a disposition response, handle it specially
            if conversation_state.get('is_disposition_response', False):
                delta = response.get('delta', '')
                conversation_state['disposition_audio_transcript'] = conversation_state.get('disposition_audio_transcript', '') + delta
                return
                
            delta = response.get('delta', '')
            conversation_state['ai_transcript'] += delta
            
        elif event_type == 'response.audio_transcript.done':
            # If this is a disposition response, handle it specially
            if conversation_state.get('is_disposition_response', False):
                transcript = response.get('transcript', '') or conversation_state.get('disposition_audio_transcript', '')
                print(f"[LOG] AI Disposition Audio Transcript: {transcript}")
                # Log to database
                await log_conversation(
                    conversation_state['lead_id'],
                    conversation_state['conversation_id'],
                    'assistant',
                    transcript
                )
                
                # Don't reset disposition tracking yet
                return
                
            transcript = response.get('transcript', '')
            if transcript:
                print(f"[LOG] AI Audio Transcript: {transcript}")
                # Log to database
                await log_conversation(
                    conversation_state['lead_id'],
                    conversation_state['conversation_id'],
                    'assistant',
                    transcript
                )
            else:
                print(f"[LOG] AI Audio Transcript: {conversation_state['ai_transcript']}")
                # Log to database
                await log_conversation(
                    conversation_state['lead_id'],
                    conversation_state['conversation_id'],
                    'assistant',
                    conversation_state['ai_transcript']
                )
            conversation_state['ai_transcript'] = ''

            
        # Handle response creation
        elif event_type == 'response.created':
            response_id = response.get('response', {}).get('id', '')
            print(f"[LOG] Response created with ID: {response_id}")
            
            # If this is a disposition response, mark it
            if conversation_state.get('is_disposition_response', False):
                print(f"[DEBUG] This is a disposition response, ID: {response_id}")
                conversation_state['disposition_response_id'] = response_id
            
            conversation_state['active_response'] = True
            
        # Handle response completion
        elif event_type == 'response.done':
            response_id = response.get('response', {}).get('id', '')
            print(f"[LOG] Response completed with ID: {response_id}")
            conversation_state['active_response'] = False
            
            # Check if this is a disposition response
            if conversation_state.get('is_disposition_response', False) and response_id == conversation_state.get('disposition_response_id', ''):
                print(f"[DEBUG] Disposition response completed, waiting for audio to be played")
                # Don't reset disposition tracking yet
                return
            
            # Log any remaining response items
            for item_id, text in conversation_state['response_items'].items():
                print(f"[LOG] AI Response (item {item_id}): {text}")
                # Log to database
                await log_conversation(
                    conversation_state['lead_id'],
                    conversation_state['conversation_id'],
                    'assistant',
                    text
                )
            conversation_state['response_items'] = {}
            
            # If there's a pending language reminder, send it now
            if conversation_state.get('pending_language_reminder', False):
                print("[LOG] Sending pending language reminder")
                await create_response_with_completion_instructions(
                    openai_ws,
                    "Politely remind the user that this conversation must be in English, then ask how you can help you today."
                )
                conversation_state['active_response'] = True
                conversation_state['pending_language_reminder'] = False
            
            # If there's a pending hangup, hang up the call after a short delay
            if conversation_state.get('pending_hangup', False):
                print(f"[LOG] Response completed, hanging up call with disposition {conversation_state['disposition']}")
                # Add a small delay to ensure the audio is fully played
                await asyncio.sleep(1)
                # Hang up the call
                await hangup_call(
                    conversation_state['conversation_id'], 
                    conversation_state['disposition'], 
                    conversation_state['lead_id'],
                    conversation_state.get('disposition_message', ''),
                    followup_datetime=conversation_state.get('followup_datetime')
                )
                conversation_state['pending_hangup'] = False
            
        # Handle user transcriptions
        elif event_type == 'conversation.item.input_audio_transcription.completed':
            # If the call is ending, ignore any more input
            if conversation_state.get('call_ending', False):
                print("[LOG] Call is ending, ignoring user input")
                return
                
            transcript = response.get('transcript', '').strip()

            # 1️⃣ Reject empty or very short transcripts
            if not transcript or len(transcript) < 2:
                print(f"[LOG] Ignored empty/short transcript: '{transcript}'")
                return

            # 2️⃣ Ignore watermark/noise artifacts
            if transcript.lower().startswith("subs by www.zeoranger.co.uk"):
                print(f"[LOG] Ignored watermark/noise transcript: '{transcript}'")
                return

            # 3️⃣ Ignore known false positives
            false_positives = {
              "yeah", "okay", "ok", "hmm", "um", "uh", "hi", "test", "testing", "thank you", "Thank you", "Bye", "Bye.", "thanks", "Much", "All right.", "Yes.", "Thank you.", "Same here.", "Good evening." 
            }
            if transcript.lower() in false_positives:
                print(f"[LOG] Ignored likely false positive: '{transcript}'")
                return

            # 4️⃣ Ignore very short noise-like inputs (1–2 short words)
            words = transcript.split()
            if len(words) <= 2 and all(len(w) <= 3 for w in words):
                print(f"[LOG] Ignored noise-like input: '{transcript}'")
                return

            # 5️⃣ (Optional) Whisper confidence check, if available
            confidence = response.get("confidence", 1.0)  # fallback = 1.0
            if confidence < 0.85:
                print(f"[LOG] Ignored low-confidence transcript ({confidence:.2f}): '{transcript}'")
                return

            # ✅ Passed filters → treat as real user input
            print(f"[User] {transcript}")
            print(f"[LOG] User Input: {transcript}")

            # Cancel timeout timer when user speaks
            if conversation_state['timeout_task'] and not conversation_state['timeout_task'].done():
                conversation_state['timeout_task'].cancel()
                print("[TIMEOUT] ❌ Timer cancelled - user responded")
                conversation_state['waiting_for_user'] = False
                conversation_state['timeout_task'] = None
            if conversation_state['are_you_there_count'] > 0:
                print(f"[TIMEOUT] Resetting 'Are you there?' counter")
                conversation_state['are_you_there_count'] = 0
                conversation_state['is_are_you_there_response'] = False 

            # Log to database
            await log_conversation(
                conversation_state['lead_id'],
                conversation_state['conversation_id'],
                'user',
                transcript
            )
            
            # Check for disposition phrases
            disposition, disposition_message, followup_datetime = check_disposition(transcript, conversation_state['lead_timezone'])
            if disposition_message:  # Only process if disposition message is not None
                print(f"[LOG] Detected disposition {disposition}: {disposition_message}")
                conversation_state['disposition'] = disposition
                conversation_state['disposition_message'] = disposition_message
                if followup_datetime:
                    print(f"[LOG] Detected follow-up datetime: {followup_datetime}")
                    conversation_state['followup_datetime'] = followup_datetime
                
                # Mark that the call is ending
                conversation_state['call_ending'] = True
                
                # Cancel any active response
                if conversation_state['active_response']:
                    print("[LOG] Cancelling active response for disposition message")
                    cancel_response = {
                        "type": "response.cancel"
                    }
                    await openai_ws.send(json.dumps(cancel_response))
                    conversation_state['active_response'] = False
                
                # Create a response that will speak the disposition message
                # await create_response_with_completion_instructions(
                #     openai_ws,
                #     f"Speak this exact message: '{disposition_message}' and then end the conversation."
                # )
                conversation_state['active_response'] = True
                conversation_state['is_disposition_response'] = True
                conversation_state['disposition_response_id'] = None  # Will be set when response.created is received
                conversation_state['disposition_audio_sent'] = False  # Track if disposition audio has been sent
                print(f"[DEBUG] Set is_disposition_response to True")
            
            # Check if user is speaking in a language other than English
            elif any(ord(char) > 127 for char in transcript):  # Check for non-ASCII characters
                print("[LOG] Non-English detected")
                
                # If there's an active response, cancel it and set a flag to send reminder later
                if conversation_state['active_response']:
                    print("[LOG] Cancelling active response to send language reminder")
                    cancel_response = {
                        "type": "response.cancel"
                    }
                    await openai_ws.send(json.dumps(cancel_response))
                    conversation_state['pending_language_reminder'] = True
                else:
                    # Send the reminder immediately
                    print("[LOG] Sending language reminder immediately")
                    await create_response_with_completion_instructions(
                        openai_ws,
                        "Politely remind the user that this conversation must be in English, then ask how you can help you today."
                    )
                    conversation_state['active_response'] = True
            
        # Handle speech started (user interruption)
        elif event_type == 'input_audio_buffer.speech_started':
            print("[TIMEOUT] User started speaking, cancelling timeout")
            # Cancel timeout timer when user starts speaking
            if conversation_state['timeout_task'] and not conversation_state['timeout_task'].done():
                conversation_state['timeout_task'].cancel()
                print("[TIMEOUT] ❌ Timer cancelled - user started speaking")
                conversation_state['waiting_for_user'] = False
                conversation_state['timeout_task'] = None
                # Reset "Are you there?" counter when user starts speaking
                if conversation_state['are_you_there_count'] > 0:
                    print(f"[TIMEOUT] User started speaking, resetting 'Are you there?' counter from {conversation_state['are_you_there_count']} to 0")
                    conversation_state['are_you_there_count'] = 0
            conversation_state['user_speaking'] = True
            if conversation_state['in_ai_response']:
                print()  # Finish current AI response line
                conversation_state['in_ai_response'] = False
                print(f"[LOG] AI Response Interrupted: {conversation_state['current_ai_text']}")
                # Log interrupted response to database
                if conversation_state['current_ai_text']:
                    await log_conversation(
                        conversation_state['lead_id'],
                        conversation_state['conversation_id'],
                        'assistant',
                        f"[INTERRUPTED] {conversation_state['current_ai_text']}"
                    )
                conversation_state['current_ai_text'] = ''
            
            clear_audio_data = {
                "event": "clearAudio",
                "stream_id": plivo_ws.stream_id
            }
            await plivo_ws.send(json.dumps(clear_audio_data))
            
            # Only cancel if there's an active response
            if conversation_state['active_response']:
                cancel_response = {
                    "type": "response.cancel"
                }
                await openai_ws.send(json.dumps(cancel_response))
                conversation_state['active_response'] = False
            else:
                print("[LOG] No active response to cancel")
        # Handle speech stopped
        elif event_type == 'input_audio_buffer.speech_stopped':
            print("[TIMEOUT] User stopped speaking")
            conversation_state['user_speaking'] = False
        # Handle response cancelled
        elif event_type == 'response.cancelled':
            response_id = response.get('response', {}).get('id', '')
            conversation_state['active_response'] = False
            print("[LOG] Response cancelled")
            
            # If there's a pending language reminder, send it now
            if conversation_state.get('pending_language_reminder', False):
                print("[LOG] Sending pending language reminder after cancellation")
                await create_response_with_completion_instructions(
                    openai_ws,
                    "Politely remind the user that this conversation must be in English, then ask how you can help you today."
                )
                conversation_state['active_response'] = True
                conversation_state['pending_language_reminder'] = False
            
        # Handle other events
        elif event_type == 'session.updated':
            print('Session updated successfully')
        elif event_type == 'error':
            print(f'Error received from realtime API: {response}')
            # Handle the specific cancellation error gracefully
            if response.get('error', {}).get('code') == 'response_cancel_not_active':
                print("[LOG] Ignoring cancellation error - no active response")
                conversation_state['active_response'] = False
            # Handle the active response error
            elif response.get('error', {}).get('code') == 'conversation_already_has_active_response':
                print("[LOG] Cannot create new response - one already active")
                # If we were trying to send a language reminder, set the pending flag
                if conversation_state.get('pending_language_reminder', False):
                    print("[LOG] Language reminder already pending")
                    
        # UPDATED AUDIO HANDLING STARTS HERE
        elif event_type == 'response.audio.delta':
            try:
                # If this is a disposition response, handle it specially
                if conversation_state.get('is_disposition_response', False):
                    # Send audio deltas for disposition responses directly to Plivo
                    audio_delta = {
                        "event": "playAudio",
                        "media": {
                            "contentType": 'audio/x-mulaw',
                            "sampleRate": 8000,
                            "payload": response['delta']  # Already base64 encoded
                        }
                    }
                    await plivo_ws.send(json.dumps(audio_delta))
                    # Don't set disposition_audio_sent here - we're still sending
                    return
                
                # For regular responses, send audio chunks immediately without buffering
                audio_delta = {
                    "event": "playAudio",
                    "media": {
                        "contentType": 'audio/x-mulaw',
                        "sampleRate": 8000,
                        "payload": response['delta']  # Already base64 encoded
                    }
                }
                await plivo_ws.send(json.dumps(audio_delta))
                
                # Track audio bytes for timing calculation
                audio_bytes = len(base64.b64decode(response['delta']))
                conversation_state['total_audio_bytes'] += audio_bytes
                
                # Set start time on first chunk
                if conversation_state['audio_start_time'] is None:
                    import time
                    conversation_state['audio_start_time'] = time.time()
                    
                # Add logging for audio chunks
                print(f"[AUDIO] Sending chunk: {len(response['delta'])} bytes, total so far: {conversation_state['total_audio_bytes']} bytes")
                    
            except Exception as e:
                print(f"[ERROR] Failed to send audio chunk: {e}")
                
        elif event_type == 'response.audio.done':
            try:
                # Calculate actual audio duration (μ-law: 8000 bytes/sec)
                audio_duration = conversation_state['total_audio_bytes'] / 8000.0
                
                # Add a fixed buffer for network/telephony latency
                total_delay = audio_duration + 2.0  # 2 second buffer
                
                print(f"[DEBUG] Audio duration: {audio_duration:.2f}s, total delay: {total_delay:.2f}s")
                
                # Create a task to handle completion after audio finishes
                async def handle_audio_completion():
                    try:
                        await asyncio.sleep(total_delay)
                        print("[LOG] ✅ AI Audio completed - Full playback finished")
                        
                        # Reset audio tracking
                        conversation_state['total_audio_bytes'] = 0
                        conversation_state['audio_start_time'] = None
                        
                        # Start timeout for user response (if not disposition)
                        if not conversation_state.get('is_disposition_response', False):
                            # Check if this was an "Are you there?" response
                            if conversation_state.get('is_are_you_there_response', False):
                                print("[TIMEOUT] 'Are you there?' audio completed, restarting timeout timer")
                                conversation_state['is_are_you_there_response'] = False
                            
                            # Start timeout timer
                            if (not conversation_state.get('pending_hangup', False) and
                                not conversation_state.get('waiting_for_user', False) and
                                not conversation_state.get('call_ending', False)):
                                print("[TIMEOUT] ⏰ Starting 5-second timeout timer")
                                conversation_state['waiting_for_user'] = True
                                conversation_state['timeout_task'] = asyncio.create_task(asyncio.sleep(5))
                                
                                # Schedule the timeout handler
                                async def timeout_wrapper():
                                    try:
                                        print("[TIMEOUT] ⏱️ Starting 5-second countdown...")
                                        await conversation_state['timeout_task']
                                        if conversation_state.get('waiting_for_user', False):
                                            print("[TIMEOUT] ⏰ 5 seconds elapsed! Handling timeout...")
                                            # Handle timeout
                                            await handle_timeout()
                                        else:
                                            print("[TIMEOUT] ⏰ Timer completed but waiting_for_user=False")
                                    except asyncio.CancelledError:
                                        print("[TIMEOUT] ❌ Timer cancelled")
                                    except Exception as e:
                                        print(f"[TIMEOUT] ❌ Timer error: {e}")
                                
                                asyncio.create_task(timeout_wrapper())
                    except Exception as e:
                        print(f"[ERROR] Error in audio completion handler: {e}")
                
                asyncio.create_task(handle_audio_completion())
                
                # If this is a disposition response, handle it specially
                if conversation_state.get('is_disposition_response', False):
                    print(f"[LOG] Disposition audio completed, scheduling hangup")
                    
                    # Only schedule hangup if not already scheduled
                    if not conversation_state.get('disposition_hangup_scheduled', False):
                        conversation_state['disposition_hangup_scheduled'] = True
                        
                        # Create a task to hang up the call after a longer delay
                        async def hangup_task():
                            try:
                                # Increase delay to 5 seconds to ensure full audio playback
                                await asyncio.sleep(5)  
                                await hangup_call(
                                    conversation_state['conversation_id'],
                                    conversation_state['disposition'],
                                    conversation_state['lead_id'],
                                    conversation_state.get('disposition_message', ''),
                                    followup_datetime=conversation_state.get('followup_datetime')
                                )
                            except Exception as e:
                                print(f"[ERROR] Failed to hang up call: {e}")
                        
                        asyncio.create_task(hangup_task())
                    return
                    
            except Exception as e:
                print(f"[ERROR] Failed to handle audio done event: {e}")
        # UPDATED AUDIO HANDLING ENDS HERE
                
        elif event_type == 'response.function_call_arguments.done':
            print(f'Received function call response: {response}')
            if response['name'] == 'calc_sum':
                output = function_call_output(json.loads(response['arguments']), response['item_id'], response['call_id'])
                await openai_ws.send(json.dumps(output))
                
                generate_response = {
                    "type": "response.create",
                    "response": {
                        "modalities": ["text", "audio"],
                        "temperature": 0.8,
                        'instructions': 'Please share the sum from the function call output with the user'
                    }
                }
                print("Sending function call response")
                await openai_ws.send(json.dumps(generate_response))
                conversation_state['active_response'] = True
                
    except Exception as e:
        print(f"Error during OpenAI's websocket communication: {e}")

if __name__ == "__main__":
    print('Starting server to handle inbound Plivo calls...')
    initialize_database()
    app.run(host='0.0.0.0', port=PORT)