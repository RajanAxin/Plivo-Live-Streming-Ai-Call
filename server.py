import plivo
from quart import Quart, websocket, Response, request
import asyncio
import websockets
import json
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
    "IMPORTANT: The conversation must be in English. If the user speaks in a language other than English, politely ask them to speak in English."
    "If the user say invalid number then do not argue with the user. No worries, sorry to bother you. Have a great day"
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
# Function to check disposition based on user input
def check_disposition(transcript):
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
        return 6, "No worries, sorry to bother you. Have a great day"
    
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
    
    # Database queries using mysql.connector
    conn = get_db_connection()
    if conn:
        try:
            prompt_text = None
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
                    cursor.execute("SELECT * FROM ai_agent_prompts WHERE ai_agent_id = %s and is_active = 1", (ai_agent['id'],))
                    ai_agent_prompt = cursor.fetchone()
                    if ai_agent_prompt:
                        prompt_text = ai_agent_prompt['prompt_text']
                        if lead_data:
                            for key, value in lead_data.items():
                                # Remove 'lead_' prefix if present
                                placeholder = f"[lead_{key}]"  # Add 'lead_' prefix to match prompt
                                prompt_text = prompt_text.replace(placeholder, str(value))
                #print(f"Prompt text: {prompt_text}")
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
                        audio_message = f"HI, This is {ai_agent['agent_name']}. Am I speaking to {lead_data['name']}?"
                        audio = "plivoai/customer1.mp3"
                        
        except Exception as e:
            print(f"Database query error: {e}")
        finally:
            if conn.is_connected():
                cursor.close()
                conn.close()
    
    lead_id = lead_data['lead_id'] if lead_data else 0
    print(f"agent_id: {ai_agent['id'] if ai_agent else 'N/A'}")
    
    # Use custom prompt if available, combined with SYSTEM_MESSAGE
    if prompt_text:
        prompt_to_use = f"{SYSTEM_MESSAGE}\n\n{prompt_text}"
    else:
        prompt_to_use = SYSTEM_MESSAGE
    # Store prompt in a global dictionary keyed by call_uuid
    if not hasattr(app, 'call_prompts'):
        app.call_prompts = {}
    app.call_prompts[call_uuid] = prompt_to_use
    
    # print(f"lead_id: {lead_id}")
    print(f"prompt_text: {prompt_to_use}")
    
    ws_url = (
    f"wss://{request.host}/media-stream?"
    f"audio_message={quote(audio_message)}"
    f"&amp;CallUUID={call_uuid}"
    f"&amp;From={from_number}"
    f"&amp;To={to_number}"
    f"&amp;lead_id={lead_id}"
    f"&amp;voice_name={voice_name}"
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
@app.websocket('/media-stream')
async def handle_message():
    print('Client connected')
    plivo_ws = websocket 
    audio_message = websocket.args.get('audio_message', "Hi this is verse How can i help you?")
    call_uuid = websocket.args.get('CallUUID', 'unknown')
    prompt_text = getattr(app, 'call_prompts', {}).get(call_uuid, SYSTEM_MESSAGE)
    voice_name = websocket.args.get('voice_name', 'alloy')
    print('audio_message', audio_message)
    print('prompt_text', prompt_text)
    print('voice_name', voice_name)
    
    # Initialize conversation state
    conversation_state = {
        'in_ai_response': False,
        'current_ai_text': '',
        'conversation_id': call_uuid,
        'from_number': websocket.args.get('From', ''),
        'to_number': websocket.args.get('To', ''),
        'lead_id': websocket.args.get('lead_id', 'unknown'),
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
    }
    
    url = "wss://api.openai.com/v1/realtime?model=gpt-4o-realtime-preview-2024-10-01"
    headers = {
        "Authorization": f"Bearer {OPENAI_API_KEY}",
        "OpenAI-Beta": "realtime=v1",
    }
    
    try: 
        async with websockets.connect(url, extra_headers=headers) as openai_ws:
            print('Connected to the OpenAI Realtime API')
            
            # Send session update first
            await send_Session_update(openai_ws, prompt_text, voice_name)
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
            # Log to database
            await log_conversation(
                conversation_state['lead_id'],
                conversation_state['conversation_id'],
                'user',
                transcript
            )
            
            # Check for disposition phrases
            disposition, disposition_message = check_disposition(transcript)
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
                
            audio_delta = {
               "event": "playAudio",
                "media": {
                    "contentType": 'audio/x-mulaw',
                    "sampleRate": 8000,
                    "payload": base64.b64encode(base64.b64decode(response['delta'])).decode('utf-8')
                }
            }
            await plivo_ws.send(json.dumps(audio_delta))
        elif event_type == 'response.audio.done':
            # If this is a disposition response, handle it specially
            if conversation_state.get('is_disposition_response', False) and conversation_state.get('disposition_audio_sent', False):
                print(f"[LOG] Disposition audio completed, hanging up call")
                
                # Reset disposition tracking
                conversation_state['is_disposition_response'] = False
                conversation_state['disposition_response_id'] = None
                conversation_state['disposition_audio_sent'] = False
                
                # Hang up the call immediately after the disposition audio is done
                print(f"[LOG] Hanging up call with disposition {conversation_state['disposition']}")
                await asyncio.sleep(4)
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