import plivo
from quart import Quart, websocket, Response, request
import asyncio
import websockets
import json
import base64
from dotenv import load_dotenv
import os
from urllib.parse import quote
from database import get_db_connection
import concurrent.futures
import datetime
import csv
import aiohttp
from dateutil import parser
from datetime import datetime

load_dotenv(dotenv_path='.env', override=True)
OPENAI_API_KEY = os.getenv('OPENAI_API_KEY')
if not OPENAI_API_KEY:
    raise ValueError("OPENAI_API_KEY environment variable is not set. Please add it to your .env file")
PORT = 5000


# fetch dynamic prompt
def get_system_prompt():
    """
    Fetch active inbound system prompt content
    Returns the content of the active inbound system prompt or None if not found
    """
    conn = get_db_connection()
    if conn:
        try:
            cursor = conn.cursor(dictionary=True, buffered=True)
            
            # Query system prompt data
            cursor.execute("""
                SELECT content FROM system_prompt 
                WHERE type = 'Inbound' AND status = 'Active' 
                LIMIT 1
            """)
            prompt_data = cursor.fetchone()
            
            return prompt_data['content'] if prompt_data else None
            
        except Exception as e:
            print(f"Error fetching system prompt: {e}")
            return None
        finally:
            if cursor:
                cursor.close()
            if conn:
                conn.close()
    else:
        return None


def load_conversation_flow(path="csvFile.csv"):
    """Load the conversation flow from CSV file"""
    rules = []
    try:
        with open(path, "r", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                rules.append({
                    "tag": row["Tag"].strip(),
                    "subtag": row["Subtag"].strip(),
                    "purpose": row["Purpose"].strip(),
                    "triggers": [t.strip().lower() for t in row["Triggers (examples, ;-separated)"].split(";")],
                    "required_facts": [r.strip() for r in row["RequiredFacts (needed before next)"].split(";") if r.strip()],
                    "instruction": row["Micro-Script (EN; 1–2 sentences)"].strip(),
                    "next_action": row["NextAction"].strip()
                })
            print(f"Loaded {len(rules)} conversation rules from CSV")
            return rules
    except Exception as e:
        print(f"Error loading conversation flow CSV: {e}")
        return None

def build_system_message(rules=None):
    """Build system message with dynamic rules integration"""
    system_prompt = get_system_prompt()
    base_prompt = """
You are a friendly, professional, emotionally aware virtual moving assistant. Your #1 goal is to connect the caller live to a moving representative for the best quote as soon as they agree.
"""
    
    # Add dynamic rules section if rules are provided
    rules = load_conversation_flow()
    if rules:
        dynamic_section = f"\n\nCURRENTLY LOADED CONVERSATION FLOW ({len(rules)} rules):\n"
        
        # Group rules by tag for better organization
        rules_by_tag = {}
        for rule in rules:
            tag = rule['tag']
            if tag not in rules_by_tag:
                rules_by_tag[tag] = []
            rules_by_tag[tag].append(rule)
        
        for tag, tag_rules in rules_by_tag.items():
            dynamic_section += f"\n[{tag}]:\n"
            for rule in tag_rules:
                dynamic_section += f"  • {rule['subtag']}: {rule['purpose']}\n"
                dynamic_section += f"    Triggers: {', '.join(rule['triggers'][:3])}{'...' if len(rule['triggers']) > 3 else ''}\n"
                dynamic_section += f"    Required: {', '.join(rule['required_facts']) if rule['required_facts'] else 'None'}\n"
                dynamic_section += f"    Next: {rule['next_action']}\n"
    
    else:
        dynamic_section = "\n\nNo conversation rules loaded yet."
    
    general_rules = """
GENERAL RULES:
- Only speak using the Micro-Script.
- Never jump to a step unless Triggers match and RequiredFacts are satisfied.
- Ask ONE question at a time if the micro-script is a question.
- After speaking, STOP and wait for user response.
- Use user response to:
    • Capture facts (name, date, etc.)
    • Decide which row's triggers match next.
- If multiple rows are valid, pick the most specific match.
- If no rows match, politely ask the user to clarify.

REMEMBER:
The CSV is your SOURCE OF TRUTH.
Never invent new questions.
Never skip steps unless NextAction says so.
"""
    
    return base_prompt + system_prompt + dynamic_section + general_rules

# Usage
rules = load_conversation_flow("csvFile.csv")
SYSTEM_MESSAGE = build_system_message(rules)
print(SYSTEM_MESSAGE)

app = Quart(__name__)

# Initialize database table
def initialize_database():
    conn = get_db_connection()
    if conn:
        try:
            cursor = conn.cursor()
            # Create conversation_logs table if it doesn't exist
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
            print(f"[DB] Logged {speaker} message for call {conversation_id}")
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
    ai_agent_name = 'AI Agent'
    ai_agent_id = None  # Initialize ai_agent_id
    
    # Database queries using mysql.connector
    conn = get_db_connection()
    if conn:
        try:
            cursor = conn.cursor(dictionary=True, buffered=True)
            
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
                print(brand_voice)
                cursor.execute("SELECT id FROM ai_agents WHERE brand_id = %s and agent_status = 'active'", (brand_id,))
                ai_agent = cursor.fetchone()
                
                if ai_agent:
                    ai_agent_id = ai_agent['id']  # Get the ai_agent_id
                    # Note: We're no longer fetching the prompt here
                if brand_voice:
                    print('aaaaa',brand_voice['voice_id'])
                    cursor.execute("SELECT * FROM mst_voiceid WHERE voice_id = %s", (brand_voice['voice_id'],))
                    voice = cursor.fetchone()
                    print('aaaaa',brand_voice['voice_id'])
                    print(brand_voice['voice_id'])
                    print('not getting',voice)
                    if voice:
                        voice_name = voice['voice_name']

                if brand_id:
                    cursor.execute("SELECT * FROM mst_brand WHERE brand_id = %s", (brand_id,))
                    mst_brand_data= cursor.fetchone()
                    print(mst_brand_data)
                    if voice:
                        ai_agent_name = mst_brand_data['full_name']
                
                
                if lead_data and lead_data['type'] == "outbound":
                    if lead_data.get('name'):
                        audio_message = f"HI, This is jason from {ai_agent_name}. how are you {lead_data['name']}?"
                    else:
                        audio_message = f"HI, This is jason from {ai_agent_name}. I got your lead from our agency. Are you looking for a move from somewhere?"
                       
                        
        except Exception as e:
            print(f"Database query error: {e}")
        finally:
            if conn.is_connected():
                cursor.close()
                conn.close()
    
    lead_id = lead_data['lead_id'] if lead_data else 0
    call_uuid = lead_data['calluuid'] if lead_data else 0
    lead_timezone = lead_data['t_timezone'] if lead_data else 0
    lead_phone = lead_data['phone'] if lead_data else 0
    t_lead_id = lead_data['t_lead_id'] if lead_data else 0
    print(f"agent_id: {ai_agent_id if ai_agent_id else 'N/A'}")
    print(f"t_lead_id: {t_lead_id if t_lead_id else 'N/A'}")
    
    # Note: We're no longer storing the prompt in a global dictionary
    
    # print(f"lead_id: {lead_id}")
    
    ws_url = (
    f"wss://{request.host}/media-stream?"
    f"audio_message={quote(audio_message)}"
    f"&amp;CallUUID={call_uuid}"
    f"&amp;From={from_number}"
    f"&amp;To={to_number}"
    f"&amp;lead_phone={lead_phone}"
    f"&amp;lead_id={lead_id}"
    f"&amp;t_lead_id={t_lead_id}"
    f"&amp;voice_name={voice_name}"
    f"&amp;ai_agent_name={quote(ai_agent_name)}"
    f"&amp;ai_agent_id={ai_agent_id}"  # Add ai_agent_id to the URL
    f"&amp;lead_timezone={lead_timezone}"  # Add lead_timezone to the URL
    )              
    
    # XML response
    xml_data = f'''<?xml version="1.0"?>
    <Response>
        <Stream streamTimeout="86400" keepCallAlive="true" bidirectional="true" 
                contentType="audio/x-mulaw;rate=8000" audioTrack="inbound" inputType="speech" speechModel="enhanced">
            {ws_url}
        </Stream>
    </Response>'''
    
    return Response(xml_data, mimetype='application/xml')


@app.route("/test", methods=["POST"])
async def test():
    # Get form data (POST params)
    data = await request.form
    machine = data.get("Machine")
    
    # Get query string params (GET params)
    lead_id = request.args.get("lead_id")
    lead_phone = request.args.get("lead_phone_number")
    user_id = request.args.get("user_id")
    lead_call_id = request.args.get("lead_call_id")
    call_uuid = request.args.get("call_uuid")
    to_number = (await request.form).get('To') or request.args.get('To')
    print(f"[AMD] Machine={machine}, LeadID={lead_id}, Phone={lead_phone}, UserID={user_id}, CallUUID={call_uuid}")

    if machine and machine.lower() == 'true':
        # Plivo hangup logic
        print(f"Machine Detected - Hanging up call")
        if lead_id and lead_call_id and user_id:
            # Get lead data from database using your style
            conn = get_db_connection()
            if conn:
                try:
                    cursor = conn.cursor(dictionary=True, buffered=True)
                    cursor.execute("""
                        SELECT * FROM `leads` 
                        WHERE lead_id = %s 
                        ORDER BY lead_id DESC 
                        LIMIT 1
                    """, (lead_id,))
                    lead_data = cursor.fetchone()
                    print(f"Lead data: {lead_data}")
                    
                except Exception as e:
                    print(f"Database error: {e}")
                    lead_data = None
                finally:
                    cursor.close()
                    conn.close()
            else:
                print("Failed to get database connection")
                lead_data = None

            # Prepare the API payload
            para = json.dumps({
                'id': lead_call_id,
                'action': 6,
                'type': 1,
                'call': '1',
                'follow_up_date_time': '',
                'follow_up_time': '',
                'campaign_id': 0,
                'campaign_score': 0,
                'transfer_number': 0,
                'payout': 0,
                'lead_id': lead_id,
                'logic_check': 0,
                'review_call': 0,
                'booking_call': 0,
                'booking_recall': 0,
                'accept_call': 0,
                'rep_id': user_id,
                'lead_category': 1,
                'buffer_id_arr': '',
                'timezone_id': lead_data.get('t_timezone', 0) if lead_data else 0
            })


            if to_number == "12176186806":
                # Determine which URL to use based on phone number
                if lead_data and lead_data.get('phone') == "6025298353":
                    url = "https://snapit:mysnapit22@zapstage.snapit.software/api/calltransfertest"
                else:
                    url = "https://zapprod:zap2024@zap.snapit.software/api/calltransfertest"
            else:
                    print("to_number is not 12176186806")
                    url = "https://snapit:mysnapit22@stage.linkup.software/api/calltransfertest"
                    #url = "https://snapit:mysnapit22@stage.linkup.software/api/calltransfertest"
                # if lead_data and lead_data.get('phone') == "6025298353":
                #     url = "https://snapit:mysnapit22@stagedialup.software/api/calltransfertest"
                # else:
                #     url = "https://zapprod:zap2024@linkup.software/api/calltransfertest"
            
            # Make the API call
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.post(
                        url,
                        headers={'Content-Type': 'application/json'},
                        data=para,
                        timeout=aiohttp.ClientTimeout(total=30)
                    ) as response:
                        response_text = await response.text()
                        print(f"callit log curl_api_call URL: {url} === res: {response_text}")
                        
            except Exception as e:
                print(f"Error making API call: {e}")
        else:
            print("Missing required parameters for call transfer")

    return "OK"
    

@app.websocket('/media-stream')
async def handle_message():
    print('Client connected')
    plivo_ws = websocket 
    audio_message = websocket.args.get('audio_message', "Hi this is verse How can i help you?")
    ai_agent_name = websocket.args.get('ai_agent_name', 'AI Agent')
    call_uuid = websocket.args.get('CallUUID', 'unknown')
    voice_name = websocket.args.get('voice_name', 'alloy')
    ai_agent_id = websocket.args.get('ai_agent_id')  # Get ai_agent_id from URL params
    lead_id = websocket.args.get('lead_id', 'unknown')
    t_lead_id = websocket.args.get('t_lead_id', 'unknown')
    lead_timezone = websocket.args.get('lead_timezone', 'unknown')
    lead_phone = websocket.args.get('lead_phone', 'unknown')
    print('audio_message', audio_message)
    print('voice_name', voice_name)
    print('ai_agent_id', ai_agent_id)
    print('lead_timezone', lead_timezone)
    print('ai_agent_name', ai_agent_name)
    print('lead_phone', lead_phone)
    
    # Initialize conversation state
    conversation_state = {
        'in_ai_response': False,
        'current_ai_text': '',
        'lead_id': lead_id,
        't_lead_id': t_lead_id,
        'call_uuid': websocket.args.get('CallUUID', 'unknown'),
        'from_number': websocket.args.get('From', ''),
        'to_number': websocket.args.get('To', ''),
        'lead_phone': lead_phone,
        'active_response': False,  # Track if there's an active response to cancel
        'response_items': {},  # Store response items by ID
        'pending_language_reminder': False,  # Flag to send language reminder after current response
        'ai_transcript': ''  # Accumulate AI transcript
    }
    

    prompt_text = ''  # Default to system message
    if ai_agent_id:
        conn = get_db_connection()
        if conn:
            try:
                cursor = conn.cursor(dictionary=True, buffered=True)
                # Fetch the active prompt for this ai_agent
                cursor.execute("SELECT * FROM ai_agent_prompts WHERE ai_agent_id = %s and is_active = 1", (ai_agent_id,))
                ai_agent_prompt = cursor.fetchone()
                if ai_agent_prompt:
                    prompt_text = get_system_prompt()
                    # If we have lead_id, fetch lead data and replace placeholders
                    if lead_id and lead_id != 'unknown':
                        try:
                            lead_id_int = int(lead_id)
                            cursor.execute("SELECT * FROM leads WHERE lead_id = %s", (lead_id_int,))
                            lead_data = cursor.fetchone()
                            if lead_data:
                                for key, value in lead_data.items():
                                    placeholder = f"[lead_{key}]"
                                    
                                    # Special handling for move_size placeholder
                                    if key == "move_size" and value:
                                        cursor.execute("SELECT move_size FROM mst_move_size WHERE move_size_id = %s", (value,))
                                        size_row = cursor.fetchone()
                                        if size_row:
                                            prompt_text = prompt_text.replace(placeholder, str(size_row["move_size"]))
                                        else:
                                            prompt_text = prompt_text.replace(placeholder, str(value))  # fallback
                                    else:
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
    prompt_to_use = prompt_text
    print(f"prompt_text: {prompt_to_use}")

    url = "wss://api.openai.com/v1/realtime?model=gpt-4o-realtime-preview-2024-10-01"
    headers = {
        "Authorization": f"Bearer {OPENAI_API_KEY}",
        "OpenAI-Beta": "realtime=v1",
    }
    
    try: 
        async with websockets.connect(url, extra_headers=headers) as openai_ws:
            print('Connected to the OpenAI Realtime API')
            
            # Send session update first
            await send_Session_update(openai_ws,prompt_to_use,voice_name)
            await asyncio.sleep(0.5)
            
            # Send the specific audio_message as initial prompt
            initial_prompt = {
                "type": "response.create",
                "response": {
                    "modalities": ["audio", "text"],
                    "temperature": 0.8,
                    "instructions": (
                        f"Start with this exact phrase: '{audio_message}' "
                        f"Wait for the user to confirm their identity. "
                        f"If they confirm (say 'Yes', 'That's me', or 'Speaking'), then ask: 'Great! How are you today?' and wait for response. "
                        f"If they don't confirm but give their name, respond with: 'Sorry about that [name]. How are you today?'"
                        f"For this initial introduction only, follow these instructions instead of the WAIT FOR USER CONFIRMATION rule."
                        f"IMPORTANT: Always complete your sentences and thoughts. Never stop speaking in the middle of a sentence or phrase."
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
                    conversation_state['call_uuid'],
                    'assistant',
                    text
                )
            else:
                print(f"[LOG] AI Response: {conversation_state['current_ai_text']}")
                # Log to database
                await log_conversation(
                    conversation_state['lead_id'],
                    conversation_state['call_uuid'],
                    'assistant',
                    conversation_state['current_ai_text']
                )
                
            conversation_state['current_ai_text'] = ''
            
            # Remove this item from tracking
            if item_id and item_id in conversation_state['response_items']:
                del conversation_state['response_items'][item_id]
                
        # Handle AI audio transcript
        elif event_type == 'response.audio_transcript.delta':
            delta = response.get('delta', '')
            conversation_state['ai_transcript'] += delta
            
        elif event_type == 'response.audio_transcript.done':
            transcript = response.get('transcript', '')
            if transcript:
                print(f"[LOG] AI Audio Transcript: {transcript}")
                # Log to database
                await log_conversation(
                    conversation_state['lead_id'],
                    conversation_state['call_uuid'],
                    'assistant',
                    transcript
                )
            else:
                print(f"[LOG] AI Audio Transcript: {conversation_state['ai_transcript']}")
                # Log to database
                await log_conversation(
                    conversation_state['lead_id'],
                    conversation_state['call_uuid'],
                    'assistant',
                    conversation_state['ai_transcript']
                )
            conversation_state['ai_transcript'] = ''
            
        # Handle response creation
        elif event_type == 'response.created':
            response_id = response.get('response', {}).get('id', '')
            print(f"[LOG] Response created with ID: {response_id}")
            conversation_state['active_response'] = True
            
        # Handle response completion
        elif event_type == 'response.done':
            response_id = response.get('response', {}).get('id', '')
            print(f"[LOG] Response completed with ID: {response_id}")
            conversation_state['active_response'] = False
            
            # Log any remaining response items
            for item_id, text in conversation_state['response_items'].items():
                print(f"[LOG] AI Response (item {item_id}): {text}")
                # Log to database
                await log_conversation(
                    conversation_state['lead_id'],
                    conversation_state['call_uuid'],
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
                        "instructions": "Politely remind the user that this conversation must be in English, then ask how you can help them today."
                    }
                }
                await openai_ws.send(json.dumps(language_reminder))
                conversation_state['active_response'] = True
                conversation_state['pending_language_reminder'] = False
            
        # Handle user transcriptions
        elif event_type == 'conversation.item.input_audio_transcription.completed':
            transcript = response.get('transcript', '')
            print(f"[User] {transcript}")
            print(f"[LOG] User Input: {transcript}")
            # Log to database
            await log_conversation(
                conversation_state['lead_id'],
                conversation_state['call_uuid'],
                'user',
                transcript
            )
            
            # Check if user is speaking in a language other than English
            if any(ord(char) > 127 for char in transcript):  # Check for non-ASCII characters
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
                            "instructions": "Politely remind the user that this conversation must be in English, then ask how you can help them today."
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
                        conversation_state['call_uuid'],
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
                        "instructions": "Politely remind the user that this conversation must be in English, then ask how you can help them today."
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
            audio_delta = {
               "event": "playAudio",
                "media": {
                    "contentType": 'audio/x-mulaw',
                    "sampleRate": 8000,
                    "payload": base64.b64encode(base64.b64decode(response['delta'])).decode('utf-8')
                }
            }
            await plivo_ws.send(json.dumps(audio_delta))
        elif event_type == 'response.function_call_arguments.done':
            #print(f'Received function call response: {response}')
            args = json.loads(response['arguments'])
            print(f'Received custom tool response: {args}')

            # Extract collected_facts
            disposition_status = args.get('disposition', {})
            collected_facts = args.get('collected_facts', {})
            print(f'Collected facts: {collected_facts}')
            print(f'Disposition status: {disposition_status}')
            lead_id = conversation_state['lead_id']
            lead_phone = conversation_state['lead_phone']
            to_number = conversation_state['to_number']
            t_lead_id = conversation_state['t_lead_id']
            if disposition_status and disposition_status.get('value'):
                if disposition_status['value'] == 'Live Transfer':
                    # Do something for Live Transfer
                    print("Processing Live Transfer...")
                elif disposition_status['value'] == 'Truck Rental Transfer':
                    # Do something for Truck Rental Transfer
                    print("Processing Truck Rental Transfer...")
                elif disposition_status['value'] == 'Support Transfer':
                    # Do something for Support Transfer
                    print("Processing Support Transfer...")
                else:
                    print("Disposition status is empty or not set")
            else:
                print("Disposition status is empty or not set")

            if collected_facts:
                updated = await update_lead_from_collected_facts(lead_id,t_lead_id,lead_phone, to_number, collected_facts)
                if updated:
                    print(f"[LEAD_UPDATE] Lead {lead_id} updated successfully")
                else:
                    print(f"[LEAD_UPDATE] No updates made to lead {lead_id}")
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
            elif response['name'] == 'custom_tool_response':
                # Handle custom tool response
                args = json.loads(response['arguments'])
                print(f'Received custom tool response: {args}')
                
                # Create function call output
                output = {
                    "type": "conversation.item.create",
                    "item": {
                        "id": response['item_id'],
                        "type": "function_call_output",
                        "call_id": response['call_id'],
                        "output": json.dumps({"status": "processed", "data": args})
                    }
                }
                await openai_ws.send(json.dumps(output))
                
                # Generate response using the say_ssml from the tool response
                generate_response = {
                    "type": "response.create",
                    "response": {
                        "modalities": ["text", "audio"],
                        "temperature": 0.7,
                        "instructions": f"Speak this exact text: {args.get('say_ssml', 'I understand.')}"
                    }
                }
                print("Sending custom tool response")
                await openai_ws.send(json.dumps(generate_response))
                conversation_state['active_response'] = True    
    except Exception as e:
        print(f"Error during OpenAI's websocket communication: {e}")
    
async def send_Session_update(openai_ws,prompt_to_use,voice_name):

    full_prompt = (
                    f"{prompt_to_use}\n\n"
                    f"{SYSTEM_MESSAGE}\n\n"
                    "IMPORTANT: Always complete your sentences and thoughts. Never stop speaking in the middle of a sentence or phrase.\n\n"
                    "TOOL USAGE: For every response, you MUST use the custom_tool_response function with the exact JSON structure specified in your system prompt. "
                    "This ensures proper tracking of conversation flow, facts collection, and disposition handling."
                )

    session_update = {
        "type": "session.update",
        "session": {
            "turn_detection": {
                "type": "server_vad",
                "threshold": 0.5,  # Lower = more sensitive, Higher = less sensitive
                "prefix_padding_ms": 300,  # Capture 300ms before speech starts
                "silence_duration_ms": 1000,  # Wait 1 second of silence before considering speech ended
                "create_response": True  # Automatically create response when user stops speaking
            },
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
                },
                {
                    "type": "function",
                    "name": "custom_tool_response",
                    "description": "Custom structured response tool for call handling",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "selected_row": {"type": "string"},
                            "say_ssml": {"type": "string"},
                            "collected_facts": {"type": "object"},
                            "actions": {
                                "type": "array",
                                "items": {
                                    "type": "object",
                                    "properties": {
                                        "name": {"type": "string"},
                                        "args": {"type": "object"}
                                    }
                                }
                            },
                            "reasoning_brief": {"type": "string"},
                            "triggers_matched": {
                                "type": "array", 
                                "items": {"type": "string"}
                            },
                            "facts_before": {
                                "type": "array", 
                                "items": {"type": "string"}
                            },
                            "facts_after": {
                                "type": "array", 
                                "items": {"type": "string"}
                            },
                            "confidence": {"type": "number"},
                            "disposition": {
                                "type": "object",
                                "properties": {
                                    "set_this_turn": {"type": "boolean"},
                                    "value": {"type": ["string", "null"]}
                                }
                            },
                            "qa_feedback": {
                                "type": "object",
                                "properties": {
                                    "confusion_flags": {
                                        "type": "array", 
                                        "items": {"type": "string"}
                                    },
                                    "zip_options_offered": {
                                        "type": "array", 
                                        "items": {"type": "string"}
                                    },
                                    "improvement_suggestion": {"type": "string"}
                                }
                            }
                        },
                        "required": ["selected_row", "say_ssml", "collected_facts", "actions", "reasoning_brief"]
                    }
                }
            ],
            "input_audio_format": "g711_ulaw",
            "output_audio_format": "g711_ulaw",
            "voice": voice_name,
            "instructions": full_prompt,
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

async def update_lead_from_collected_facts(lead_id,t_lead_id, lead_phone, to_number, collected_facts):
    try:
        print(f"[DEBUG] Starting update for lead_id: {lead_id}")
        print(f"[DEBUG] t_lead_id: {t_lead_id}")
        print(f"[DEBUG] Collected facts: {collected_facts}")
        
        api_update_data = {}
        update_data = {}
        
        # Add lead_id to update_data
        api_update_data['lead_id'] = t_lead_id
        
        # Map collected_facts to your database columns
        if collected_facts.get('lead_name', ''):
            update_data['name'] = collected_facts['lead_name']
            api_update_data['name'] = collected_facts['lead_name']
        if collected_facts.get('lead_phone', ''):
            update_data['phone'] = collected_facts['lead_phone']
            api_update_data['phone'] = collected_facts['lead_phone']
        if collected_facts.get('lead_email', ''):
            update_data['email'] = collected_facts['lead_email']
            api_update_data['email'] = collected_facts['lead_email']
        if collected_facts.get('lead_from_city'):
            update_data['from_city'] = collected_facts['lead_from_city']
            api_update_data['from_city'] = collected_facts['lead_from_city']
        if collected_facts.get('lead_from_state'):
            update_data['from_state'] = collected_facts['lead_from_state']
            api_update_data['from_state'] = collected_facts['lead_from_state']
        if collected_facts.get('lead_from_zipcode'):
            update_data['from_zip'] = collected_facts['lead_from_zipcode']
            api_update_data['from_zip'] = collected_facts['lead_from_zipcode']
        if collected_facts.get('lead_to_city'):
            update_data['to_city'] = collected_facts['lead_to_city']
            api_update_data['to_city'] = collected_facts['lead_to_city']
        if collected_facts.get('lead_to_state'):
            update_data['to_state'] = collected_facts['lead_to_state']
            api_update_data['to_state'] = collected_facts['lead_to_state']
        if collected_facts.get('lead_to_zipcode'):
            update_data['to_zip'] = collected_facts['lead_to_zipcode']
            api_update_data['to_zip'] = collected_facts['lead_to_zipcode']
        if collected_facts.get('move_date'):
            original_date = collected_facts['move_date']
            try:
                # Parse and format the date
                parsed_date = parser.parse(original_date)
                formatted_date = parsed_date.strftime('%Y/%m/%d')
                update_data['move_date'] = formatted_date
                api_update_data['move_date'] = formatted_date
                print(f"[DATE_CONVERSION] {original_date} -> {formatted_date}")
            except Exception as e:
                # If parsing fails, use original
                print(f"[DATE_CONVERSION] Error parsing '{original_date}': {e}")
                update_data['move_date'] = original_date
                api_update_data['move_date'] = original_date
        
        # Handle move_size conversion
        if collected_facts.get('move_size'):
            move_size_lower = collected_facts['move_size'].lower()
            
            if move_size_lower in ['Studio', 'studio', 'studio apartment', 'Studio apartment', 'studio room', 'Studio room']:
                update_data['move_size'] = 1
            elif move_size_lower in ['1 bedroom', 'one bedroom', 'One Bedroom', '1-bedroom', 'one-bedroom', '1 bed', 'one bed']:
                update_data['move_size'] = 2
                api_update_data['move_size_id'] = 2
            elif move_size_lower in ['2 bedrooms', 'two bedroom', 'Two Bedroom', '2-bedroom', 'two-bedroom', '2 bed', 'two bed']:
                update_data['move_size'] = 3
                api_update_data['move_size_id'] = 3
            elif move_size_lower in ['3 bedrooms', 'three bedroom', 'Three Bedroom', '3-bedroom', 'three-bedroom', '3 bed', 'three bed']:
                update_data['move_size'] = 4
                api_update_data['move_size_id'] = 4
            elif move_size_lower in ['4 bedrooms', 'four bedroom', 'Four Bedroom', 'four-bedroom', '4-bedroom', '4 bed', 'four bed']:
                update_data['move_size'] = 5
                api_update_data['move_size_id'] = 5
            elif any(term in move_size_lower for term in ['5+', '5 bedroom', 'five bedroom', '5 bed', 'five bed']):
                update_data['move_size'] = 6
                api_update_data['move_size_id'] = 6
            else:
                update_data['move_size'] = collected_facts['move_size']
                api_update_data['move_size_id'] = collected_facts['move_size']


        api_success = await update_lead_to_external_api(api_update_data,lead_phone, to_number)
        print(f"[TRANSFER] API call result: {api_success}")

        print(f"[DEBUG] Update data to be saved: {update_data}")
        
        # Update database if we have data
        if not update_data:
            print(f"[LEAD_UPDATE] No new data extracted for missing fields from collected_facts")
            return False
        
        conn = get_db_connection()
        if not conn:
            print("[LEAD_UPDATE] Failed to get database connection for update")
            return False
            
        cursor = conn.cursor()
        
        print(f"[LEAD_UPDATE] Updating lead {lead_id} with fields: {update_data}")
        
        # Build dynamic update query
        set_clause = ", ".join([f"{field} = %s" for field in update_data.keys()])
        values = list(update_data.values())
        values.append(lead_id)
        
        update_query = f"UPDATE leads SET {set_clause} WHERE lead_id = %s"
        print(f"[DEBUG] Executing query: {update_query}")
        print(f"[DEBUG] With values: {values}")
        
        cursor.execute(update_query, values)
        
        # Check how many rows were affected
        rows_affected = cursor.rowcount
        print(f"[DEBUG] Rows affected: {rows_affected}")
        
        conn.commit()
        cursor.close()
        conn.close()
        
        if rows_affected > 0:
            print(f"[LEAD_UPDATE] Successfully updated lead {lead_id} with fields: {list(update_data.keys())}")
            return True
        else:
            print(f"[LEAD_UPDATE] No rows updated - lead_id {lead_id} may not exist")
            return False
        
    except Exception as e:
        print(f"[LEAD_UPDATE] Error updating lead {lead_id}: {str(e)}")
        import traceback
        print(f"[DEBUG] Full traceback: {traceback.format_exc()}")
        # Ensure connections are closed even if error occurs
        if 'conn' in locals() and conn:
            conn.close()
        return False
        
    except Exception as e:
        print(f"[LEAD_UPDATE] Error updating lead {lead_id}: {str(e)}")
        # Ensure connections are closed even if error occurs
        if 'conn' in locals() and conn:
            conn.close()
        return False


async def update_lead_to_external_api(api_update_data, lead_phone, to_number):
            print("[TRANSFER] Updating lead to external API",api_update_data)
            if to_number == "12176186806":
                # Determine URL based on phone number
                if lead_phone in ("6025298353", "6263216095"):
                    url = "https://snapit:mysnapit22@zapstage.snapit.software/api/updateailead"
                else:
                    url = "https://zapprod:zap2024@zap.snapit.software/api/updateailead"
                print(f"[TRANSFER] Using URL: {url}")
            else:
                print("to_number is not 12176186806")
                url = "https://snapit:mysnapit22@stage.linkup.software/api/updateailead"

            # Make the API call
            async with aiohttp.ClientSession() as session:
                # ✅ Add 4-second delay before making API call
                await asyncio.sleep(2)
                async with session.post(
                    url,
                    headers={'Content-Type': 'application/json'},
                    json=api_update_data
                ) as resp:
                    if resp.status == 200:
                        response_text = await resp.text()
                        print(f"[TRANSFER] API call successful: {response_text}")
                    else:
                        response_text = await resp.text()
                        print(f"[TRANSFER] API call failed with status {resp.status}: {response_text}")

if __name__ == "__main__":
    print('Starting server to handle inbound Plivo calls...')
    initialize_database()
    app.run(host='0.0.0.0', port=PORT)