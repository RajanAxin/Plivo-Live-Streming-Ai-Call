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
load_dotenv(dotenv_path='.env', override=True)
OPENAI_API_KEY = os.getenv('OPENAI_API_KEY')
PLIVO_AUTH_ID = os.getenv('PLIVO_AUTH_ID')
PLIVO_AUTH_TOKEN = os.getenv('PLIVO_AUTH_TOKEN')
if not OPENAI_API_KEY:
    raise ValueError("OPENAI_API_KEY environment variable is not set. Please add it to your .env file")
PORT = 5000
SYSTEM_MESSAGE = (
    "CRITICAL: After each response, STOP speaking and WAIT for the user to respond. Do NOT continue talking or ask follow-up questions unless the user responds first. "
    "CRITICAL: When asking how the user is doing, ONLY say exactly one of: 'How are you?' or, if you know their first name, 'Hi <name>. How are you?'. Do NOT attach any other sentence, reason, or context to that utterance. Then STOP and wait for the user to respond. "
    "Do not repeat the same response back-to-back (e.g., avoid sending 'Sorry to bother you, I will call you later' twice in a row)."
    "IMPORTANT: The conversation must be in English. If the user speaks in a language other than English, politely ask them to speak in English. "
    "IMPORTANT: If the user says invalid number, wrong number, already booked, booked then or incorrect number then politely respond with: 'No worries, sorry to bother you. Have a great day'. "
    "IMPORTANT: If the user says don't call, do not call, not to call, not looking to move, not looking, not interested or take me off then politely respond with: 'No worries, sorry to bother you. Have a great day'. "
    "IMPORTANT: If the user asks for truck rental, van rental, or truck rent then politely respond with: 'We are providing moving services, sorry to bother you. Have a great day'. "
    "IMPORTANT: If the user says bye, goodbye, good bye, take care, or see you then politely respond with: 'Nice to talk with you. Have a great day'."
    "IMPORTANT: If the user says are busy, am busy, busy, call me later, call me, call me at, not available, voicemail, or asks to call later then politely respond with: 'I will call you later. Nice to talk with you. Have a great day.'."
    "Be helpful and professional in your responses. Wait for the user to speak before responding."
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
async def hangup_call(call_uuid, disposition, lead_id, text_message="I have text"):
    if not PLIVO_AUTH_ID or not PLIVO_AUTH_TOKEN:
        print("Plivo credentials not set. Cannot hang up call.")
        return
    
    # 1️⃣ Hang up the call via Plivo API
    url = f"https://api.plivo.com/v1/Account/{PLIVO_AUTH_ID}/Call/{call_uuid}/"
    auth_string = f"{PLIVO_AUTH_ID}:{PLIVO_AUTH_TOKEN}"
    auth_header = base64.b64encode(auth_string.encode()).decode()
    print(f"[DEBUG] Attempting to hang up call {call_uuid}")
    print(f"[DEBUG] URL: {url}")
    print(f"[DEBUG] Auth header: {auth_header[:10]}...")
    
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
    redirect_url = f"http://54.176.128.91/disposition_route?{urlencode(params)}"
    escaped_url = html.escape(redirect_url, quote=True)
    
    # 3️⃣ Build XML Response
    content = f'''<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Play>{text_message}</Play>
    <Redirect method="POST">{escaped_url}</Redirect>
</Response>'''
    return Response(text=content, status=200, content_type="text/xml")



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
        return 2, "No worries, sorry to bother you. Have a great day"
    
    # Pattern 2: Wrong number
    elif re.search(r"\b(wrong number|invalid number|incorrect number)\b", transcript_lower):
        return 7, "No worries, sorry to bother you. Have a great day"
    
    # Pattern 3: Not interested
    elif re.search(r"\b(not looking to move|not looking|not interested)\b", transcript_lower):
        return 3, "No worries, sorry to bother you. Have a great day"
    
    # Pattern 4: Not available
    elif re.search(r"\b(not available|hang up or press|reached the maximum time allowed to make your recording|at the tone|record your message|voicemail|voice mail|leave your message|are busy|am busy|busy|call me later|call me|call me at)\b", transcript_lower):
        
        # Check if it's a busy/call me later pattern that might have a datetime
        if re.search(r"\b(are busy|am busy|busy|call me later|call me|call me at)\b", transcript_lower):
            followup_datetime = get_followup_datetime(transcript_lower, lead_timezone)
            print(f'🎤 Lead Timezone: {lead_timezone}')
            print(f'🎤 Followup DateTime: {followup_datetime}')
            
            if followup_datetime:
                return 4, "I will call you later. Nice to talk with you. Have a great day."
        
        # Default response for voicemail or no datetime found
        return 6, "I will call you later. Nice to talk with you. Have a great day."
    
    # Pattern 5: Truck rental
    elif re.search(r"\b(truck rental|looking for truck rent|truck rent|van rental|van rent)\b", transcript_lower):
        return 13, "We are providing moving services, sorry to bother you. Have a great day"
    
    # Pattern 6: Already booked
    elif re.search(r"\b(already booked|booked)\b", transcript_lower):
        return 8, "No worries, sorry to bother you. Have a great day"
    
    # Pattern 7: Goodbye
    elif re.search(r"\b(bye|goodbye|good bye|take care|see you)\b", transcript_lower):
        return 6, "Nice to talk with you. Have a great day"
    
    # Default disposition
    return 6, None

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
        
@app.route("/answer", methods=["GET", "POST"])
async def home():
    # Extract the caller's number (From) and your Plivo number (To)
    from_number = (await request.form).get('From') or request.args.get('From')
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
        """Handle 10-second timeout by sending 'Are you there?' message"""
        try:
            conversation_state['are_you_there_count'] += 1
            import time
            current_time = time.strftime("%H:%M:%S", time.localtime())
            print(f"[TIMEOUT] 10 seconds elapsed at {current_time} without user response, sending 'Are you there?' (attempt {conversation_state['are_you_there_count']}/{conversation_state['max_are_you_there']})")

            # Reset timeout state
            conversation_state['timeout_task'] = None
            conversation_state['waiting_for_user'] = False

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
                        "temperature": 0.8,
                        "instructions": "Say exactly: 'Thank you for your time. Have a great day.' and then end the conversation."
                    }
                }
                await openai_ws.send(json.dumps(goodbye_response))
                conversation_state['active_response'] = True
                conversation_state['is_disposition_response'] = True
                return

            # Create timeout response for "Are you there?"
            timeout_response = {
                "type": "response.create",
                "response": {
                    "modalities": ["text", "audio"],
                    "temperature": 0.8,
                    "instructions": "Say exactly: 'Are you there?' in a friendly tone and wait for their response."
                }
            }
            await openai_ws.send(json.dumps(timeout_response))
            conversation_state['active_response'] = True

        except Exception as e:
            print(f"[TIMEOUT] Error handling timeout: {e}")

    def start_timeout_timer():
        """Start a 10-second timer for user response"""
        # Cancel existing timer if any
        if conversation_state['timeout_task'] and not conversation_state['timeout_task'].done():
            print("[TIMEOUT] Cancelling existing timeout task")
            conversation_state['timeout_task'].cancel()

        # Only start timer if not in a disposition flow and not already waiting
        if (not conversation_state.get('is_disposition_response', False) and
            not conversation_state.get('pending_hangup', False) and
            not conversation_state.get('waiting_for_user', False)):

            print("[TIMEOUT] ⏰ Starting 10-second timeout timer")
            conversation_state['waiting_for_user'] = True
            conversation_state['timeout_task'] = asyncio.create_task(asyncio.sleep(10))

            # Schedule the timeout handler
            async def timeout_wrapper():
                try:
                    print("[TIMEOUT] ⏱️ Starting 10-second countdown...")
                    await conversation_state['timeout_task']
                    if conversation_state.get('waiting_for_user', False):
                        print("[TIMEOUT] ⏰ 10 seconds elapsed! Calling handle_timeout()")
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
            
            # Send the specific audio_message as initial prompt
            initial_prompt = {
                "type": "response.create",
                "response": {
                    "modalities": ["audio", "text"],
                    "temperature": 0.8,
                    "instructions": (
                        f"DO NOT ask for identity confirmation. "
                        f"Start with this exact phrase: '{audio_message}' "
                        f"Then continue naturally."
                    )
                }
            }
            await openai_ws.send(json.dumps(initial_prompt))
            conversation_state['active_response'] = True  # Mark that we have an active response
            
            receive_task = asyncio.create_task(receive_from_plivo(plivo_ws, openai_ws))
            
            async for message in openai_ws:
                await receive_from_openai(message, plivo_ws, openai_ws, conversation_state)
            
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

async def receive_from_openai(message, plivo_ws, openai_ws, conversation_state):
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
                
                # Reset disposition tracking
                conversation_state['is_disposition_response'] = False
                conversation_state['disposition_text'] = ''
                
                # Don't hang up here, wait for audio to be played
                return
                
            item_id = response.get('item_id', '')
            text = response.get('text', '')
            
            print()  # Newline after AI response
            conversation_state['in_ai_response'] = False
            
            # Use the text from the event if available, otherwise use accumulated text
            if text:
                print(f"[LOG] AI Response: {text}")
                # Log to database
                await log_conversation(
                    conversation_state['lead_id'],
                    conversation_state['conversation_id'],
                    'assistant',
                    text
                )
            else:
                print(f"[LOG] AI Response: {conversation_state['current_ai_text']}")
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
                
                # Reset disposition tracking
                conversation_state['disposition_audio_transcript'] = ''
                
                # Don't hang up here, wait for audio to be played
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
                # Don't hang up here, wait for audio to be played
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
                language_reminder = {
                    "type": "response.create",
                    "response": {
                        "modalities": ["text", "audio"],
                        "temperature": 0.8,
                        "instructions": "Politely remind the user that this conversation must be in English, then ask how you can help you today."
                    }
                }
                await openai_ws.send(json.dumps(language_reminder))
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
                    conversation_state.get('disposition_message', '')
                )
                conversation_state['pending_hangup'] = False
            
        # Handle user transcriptions
        elif event_type == 'conversation.item.input_audio_transcription.completed':
            transcript = response.get('transcript', '')
            print(f"[User] {transcript}")
            print(f"[LOG] User Input: {transcript}")

            # Cancel timeout timer when user speaks
            if conversation_state['timeout_task'] and not conversation_state['timeout_task'].done():
                conversation_state['timeout_task'].cancel()
                print("[TIMEOUT] ❌ Timer cancelled - user responded")
                conversation_state['waiting_for_user'] = False
                conversation_state['timeout_task'] = None

                # Reset "Are you there?" counter when user responds
                if conversation_state['are_you_there_count'] > 0:
                    print(f"[TIMEOUT] User responded, resetting 'Are you there?' counter from {conversation_state['are_you_there_count']} to 0")
                    conversation_state['are_you_there_count'] = 0

            # Log to database
            await log_conversation(
                conversation_state['lead_id'],
                conversation_state['conversation_id'],
                'user',
                transcript
            )
            
            # Check for disposition phrases
            disposition, disposition_message = check_disposition(transcript, conversation_state['lead_timezone'])
            if disposition_message:  # Only process if disposition message is not None
                print(f"[LOG] Detected disposition {disposition}: {disposition_message}")
                conversation_state['disposition'] = disposition
                conversation_state['disposition_message'] = disposition_message
                
                # Cancel any active response
                if conversation_state['active_response']:
                    print("[LOG] Cancelling active response for disposition message")
                    cancel_response = {
                        "type": "response.cancel"
                    }
                    await openai_ws.send(json.dumps(cancel_response))
                    conversation_state['active_response'] = False
                
                # Create a conversation item with the disposition message
                item = {
                    "type": "conversation.item.create",
                    "item": {
                        "type": "message",
                        "role": "assistant",
                        "content": [
                            {
                                "type": "input_text",
                                "text": disposition_message
                            }
                        ]
                    }
                }
                await openai_ws.send(json.dumps(item))
                
                # Then create a response that will speak this message
                response_create = {
                    "type": "response.create",
                    "response": {
                        "modalities": ["text", "audio"],
                        "instructions": f"Speak this exact message: '{disposition_message}' and then end the conversation."
                    }
                }
                await openai_ws.send(json.dumps(response_create))
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
                    language_reminder = {
                        "type": "response.create",
                        "response": {
                            "modalities": ["text", "audio"],
                            "temperature": 0.8,
                            "instructions": "Politely remind the user that this conversation must be in English, then ask how you can help you today."
                        }
                    }
                    await openai_ws.send(json.dumps(language_reminder))
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
                language_reminder = {
                    "type": "response.create",
                    "response": {
                        "modalities": ["text", "audio"],
                        "temperature": 0.8,
                        "instructions": "Politely remind the user that this conversation must be in English, then ask how you can help you today."
                    }
                }
                await openai_ws.send(json.dumps(language_reminder))
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
        elif event_type == 'response.audio.delta':
            # If this is a disposition response, handle it specially
            if conversation_state.get('is_disposition_response', False):
                # Send audio deltas for disposition responses to Plivo
                audio_delta = {
                   "event": "playAudio",
                    "media": {
                        "contentType": 'audio/x-mulaw',
                        "sampleRate": 8000,
                        "payload": base64.b64encode(base64.b64decode(response['delta'])).decode('utf-8')
                    }
                }
                await plivo_ws.send(json.dumps(audio_delta))
                conversation_state['disposition_audio_sent'] = True
                return

            # Buffer audio chunks for smoother playback
            try:
                # Decode the audio chunk and add to buffer
                audio_data = base64.b64decode(response['delta'])
                conversation_state['audio_buffer'] += audio_data
                conversation_state['total_audio_bytes'] += len(audio_data)

                # Mark audio start time on first chunk
                if conversation_state['audio_start_time'] is None:
                    import time
                    conversation_state['audio_start_time'] = time.time()

                # Function to flush the audio buffer
                async def flush_audio_buffer():
                    if conversation_state['audio_buffer']:
                        # Send the buffered audio as one chunk
                        encoded_payload = base64.b64encode(conversation_state['audio_buffer']).decode('utf-8')
                        audio_delta = {
                           "event": "playAudio",
                            "media": {
                                "contentType": 'audio/x-mulaw',
                                "sampleRate": 8000,
                                "payload": encoded_payload
                            }
                        }
                        await plivo_ws.send(json.dumps(audio_delta))
                        conversation_state['audio_buffer'] = b''  # Clear buffer
                        conversation_state['audio_buffer_timer'] = None

                # Cancel existing timer and set new one
                if conversation_state['audio_buffer_timer']:
                    conversation_state['audio_buffer_timer'].cancel()

                # Flush buffer after 50ms or when it gets large enough
                buffer_size = len(conversation_state['audio_buffer'])
                if buffer_size > 1600:  # ~100ms of audio at 8kHz μ-law
                    await flush_audio_buffer()
                else:
                    # Set timer to flush after 50ms
                    conversation_state['audio_buffer_timer'] = asyncio.create_task(
                        asyncio.sleep(0.05)
                    )

                    async def delayed_flush():
                        try:
                            await conversation_state['audio_buffer_timer']
                            await flush_audio_buffer()
                        except asyncio.CancelledError:
                            pass

                    asyncio.create_task(delayed_flush())

            except Exception as e:
                print(f"[ERROR] Failed to buffer audio chunk: {e}")
        elif event_type == 'response.audio.done':
            # Flush any remaining audio buffer
            if conversation_state['audio_buffer']:
                try:
                    encoded_payload = base64.b64encode(conversation_state['audio_buffer']).decode('utf-8')
                    audio_delta = {
                       "event": "playAudio",
                        "media": {
                            "contentType": 'audio/x-mulaw',
                            "sampleRate": 8000,
                            "payload": encoded_payload
                        }
                    }
                    await plivo_ws.send(json.dumps(audio_delta))
                    conversation_state['audio_buffer'] = b''
                except Exception as e:
                    print(f"[ERROR] Failed to flush final audio buffer: {e}")

            # Cancel any pending buffer timer
            if conversation_state['audio_buffer_timer']:
                conversation_state['audio_buffer_timer'].cancel()
                conversation_state['audio_buffer_timer'] = None

            # Calculate proper delay based on actual audio length
            async def delayed_completion_log():
                # Calculate audio duration: μ-law is 8000 samples/sec, 1 byte per sample
                audio_duration_seconds = conversation_state['total_audio_bytes'] / 8000.0

                # Calculate how much audio should still be playing
                import time
                elapsed_since_start = time.time() - (conversation_state['audio_start_time'] or time.time())
                remaining_audio_time = max(0, audio_duration_seconds - elapsed_since_start)

                # Add buffer for telephony delay
                total_delay = remaining_audio_time + 0.5  # 500ms telephony buffer

                print(f"[DEBUG] Audio duration: {audio_duration_seconds:.2f}s, elapsed: {elapsed_since_start:.2f}s, waiting: {total_delay:.2f}s")
                await asyncio.sleep(total_delay)
                print("[LOG] ✅ AI Audio completed - AI has finished speaking and audio playback complete")

                # Reset audio tracking
                conversation_state['total_audio_bytes'] = 0
                conversation_state['audio_start_time'] = None

                # Start 10-second timeout timer for user response
                if not conversation_state.get('is_disposition_response', False):
                    # Check if this was an "Are you there?" response
                    if conversation_state.get('is_are_you_there_response', False):
                        print("[TIMEOUT] 'Are you there?' audio completed, restarting timeout timer")
                        conversation_state['is_are_you_there_response'] = False

                    # Start timer inline
                    if (not conversation_state.get('pending_hangup', False) and
                        not conversation_state.get('waiting_for_user', False)):

                        print("[TIMEOUT] ⏰ Starting 10-second timeout timer")
                        conversation_state['waiting_for_user'] = True
                        conversation_state['timeout_task'] = asyncio.create_task(asyncio.sleep(10))

                        # Schedule the timeout handler
                        async def timeout_wrapper():
                            try:
                                print("[TIMEOUT] ⏱️ Starting 10-second countdown...")
                                await conversation_state['timeout_task']
                                if conversation_state.get('waiting_for_user', False):
                                    print("[TIMEOUT] ⏰ 10 seconds elapsed! Handling timeout...")

                                    # Handle timeout inline
                                    conversation_state['are_you_there_count'] += 1
                                    import time
                                    current_time = time.strftime("%H:%M:%S", time.localtime())
                                    print(f"[TIMEOUT] 10 seconds elapsed at {current_time} without user response, sending 'Are you there?' (attempt {conversation_state['are_you_there_count']}/{conversation_state['max_are_you_there']})")

                                    # Reset timeout state
                                    conversation_state['timeout_task'] = None
                                    conversation_state['waiting_for_user'] = False

                                    # Check if we've reached the maximum attempts
                                    if conversation_state['are_you_there_count'] >= conversation_state['max_are_you_there']:
                                        print(f"[TIMEOUT] Reached maximum 'Are you there?' attempts ({conversation_state['max_are_you_there']}), disconnecting call")

                                        # Set disposition for no response and prepare to hang up
                                        conversation_state['disposition'] = 6
                                        conversation_state['disposition_message'] = "Thank you for your time. Have a great day."

                                        # Create final goodbye message before hanging up
                                        goodbye_response = {
                                            "type": "response.create",
                                            "response": {
                                                "modalities": ["text", "audio"],
                                                "temperature": 0.8,
                                                "instructions": "Say exactly: 'Thank you for your time. Have a great day.' and then end the conversation."
                                            }
                                        }
                                        await openai_ws.send(json.dumps(goodbye_response))
                                        conversation_state['active_response'] = True
                                        conversation_state['is_disposition_response'] = True
                                    else:
                                        # Create timeout response for "Are you there?"
                                        timeout_response = {
                                            "type": "response.create",
                                            "response": {
                                                "modalities": ["text", "audio"],
                                                "temperature": 0.8,
                                                "instructions": "Say exactly: 'Are you there?' in a friendly tone and wait for their response."
                                            }
                                        }
                                        await openai_ws.send(json.dumps(timeout_response))
                                        conversation_state['active_response'] = True
                                        conversation_state['is_are_you_there_response'] = True
                                else:
                                    print("[TIMEOUT] ⏰ Timer completed but waiting_for_user=False")
                            except asyncio.CancelledError:
                                print("[TIMEOUT] ❌ Timer cancelled")
                            except Exception as e:
                                print(f"[TIMEOUT] ❌ Timer error: {e}")

                        asyncio.create_task(timeout_wrapper())

            asyncio.create_task(delayed_completion_log())

            # If this is a disposition response, handle it specially
            if conversation_state.get('is_disposition_response', False) and conversation_state.get('disposition_audio_sent', False):
                print(f"[LOG] Disposition audio completed, hanging up call")

                # Reset disposition tracking
                conversation_state['is_disposition_response'] = False
                conversation_state['disposition_response_id'] = None
                conversation_state['disposition_audio_sent'] = False

                # Hang up the call immediately after the disposition audio is done
                print(f"[LOG] Hanging up call with disposition {conversation_state['disposition']}")
                await asyncio.sleep(5)
                await hangup_call(
                    conversation_state['conversation_id'],
                    conversation_state['disposition'],
                    conversation_state['lead_id'],
                    conversation_state.get('disposition_message', '')
                )
                return
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

async def send_Session_update(openai_ws, prompt_text, voice_name):
    session_update = {
        "type": "session.update",
        "session": {
            "turn_detection": {"type": "server_vad"},
            "tools": [
                {
                    "type": "function",
                    "name": "calc_sum",
                    "description": "Get the sum of two numbers",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "num1": { "type": "string", "description": "the first number" },
                            'num2': { "type": "string", "description": "the seconds number" }
                        },
                        "required": ["num1", "num2"]
                    }
                }
            ],
            "input_audio_format": "g711_ulaw",
            "output_audio_format": "g711_ulaw",
            "voice": voice_name,
            "instructions": prompt_text,
            "modalities": ["text", "audio"],
            "temperature": 0.8,
            "input_audio_transcription": {"model": "whisper-1"}  # Enable transcription
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

if __name__ == "__main__":
    print('Starting server to handle inbound Plivo calls...')
    initialize_database()
    app.run(host='0.0.0.0', port=PORT)