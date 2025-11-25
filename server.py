import plivo
from quart import Quart, websocket, Response, request
import asyncio
import websockets
import json
import re
import concurrent.futures
import base64
import openai
import requests
import aiohttp
from database import get_db_connection
from urllib.parse import quote, urlencode
from dotenv import load_dotenv
from dateutil import parser
import os

load_dotenv(dotenv_path='.env', override=True)

OPENAI_API_KEY = os.getenv('OPENAI_API_KEY')
if not OPENAI_API_KEY:
    raise ValueError("OPENAI_API_KEY environment variable is not set. Please add it to your .env file")

PORT = 5000

app = Quart(__name__)

def download_file(url, save_as="input.mp3"):
    """Download MP3 from URL"""
    response = requests.get(url, stream=True)
    if response.status_code == 200:
        with open(save_as, "wb") as f:
            f.write(response.content)
        return save_as
    else:
        raise Exception(f"Failed to download file: {response.status_code}")

def transcribe(file_path):
    """Transcribe audio using OpenAI Whisper"""
    with open(file_path, "rb") as f:
        transcript = openai.audio.transcriptions.create(
            model="whisper-1",
            file=f
        )
    return transcript.text

def segment_speakers(transcript_text: str):
    """Ask GPT to split transcript into Agent/Customer speakers"""
    response = openai.chat.completions.create(
        model="gpt-4o-mini",
        messages=[
            {"role": "system", "content": "You are a call transcript formatter."},
            {"role": "user", "content": f"""
            Split this transcript into two speakers: Agent and Customer.
            Keep the order of the conversation, and don't add extra text.
            Keep the correct back-and-forth flow. 
            In the conversation last if you recived Hello only then consider Only Call.
            Transcript:
            {transcript_text}
            After splitting, analyze the conversation and return the final disposition in JSON.

        Possible dispositions are:
        - Not Connected
        - Live Transfer
        - DNC
        - Not Interested
        - Follow Up
        - No Buyer
        - Voice Message
        - Wrong Phone
        - Booked
        - Booked with Us
        - Booked with PODs
        - Booked with Truck Rental
        - Truck Rental
        - IB Pickup
        - Only Call
        - No Answer
        - Business Relay
        Output format (JSON only):
        {{
            "conversation": [
                    {{ "speaker": "Agent", "text": "..." }},
                    {{ "speaker": "Customer", "text": "..." }}
                ],
                "disposition": "<one_of_the_above>"
        }}
            """}
        ],
        response_format={"type": "json_object"}  # force valid JSON
    )
    return response.choices[0].message.content

@app.get("/disposition_process")
def disposition_process():
    try:
        # Get the mp3_url from query parameters
        mp3_url = request.args.get('mp3_url')
        
        if not mp3_url:
            return {"error": "mp3_url parameter is required"}, 400
        
        file_path = download_file(mp3_url)
        text = transcribe(file_path)
        result_json = segment_speakers(text)
        os.remove(file_path)
        # Parse the JSON string to a Python dictionary
        result_dict = json.loads(result_json)
        
        # Return the parsed dictionary
        return result_dict
        
    except Exception as e:
        return {"error": str(e)}


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

            cursor.execute("""
                CREATE TABLE IF NOT EXISTS function_responses (
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    lead_id INT NOT NULL DEFAULT '0',
                    function_response JSON NOT NULL,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    INDEX (lead_id)
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


def ensure_dict(val):
    # If it's already a dict → return as is
    if isinstance(val, dict):
        return val

    # If it's a string → try to extract JSON
    if isinstance(val, str):
        # Try direct JSON parse
        try:
            return json.loads(val)
        except:
            pass

        # Try extracting {...} JSON substring from logs
        json_match = re.search(r'\{.*\}', val, re.DOTALL)
        if json_match:
            try:
                return json.loads(json_match.group(0))
            except:
                pass

        # Fallback if nothing works
        return {
            "status": "FAILURE",
            "error": val
        }

    # Any unknown type → fallback
    return {
        "status": "FAILURE",
        "error": str(val)
    }


async def log_to_dashboard(event_type, text, conversation_state):
    try:
        client = openai.OpenAI(api_key=OPENAI_API_KEY)
        client.responses.create(
            model="gpt-4.1",
            input=[
                {"role": "system", "content": f"Realtime event: {event_type}"},
                {
                    "role": "user",
                    "content": (
                        f"Lead ID: {conversation_state['lead_id']}, "
                        f"Call UUID: {conversation_state['call_uuid']}, "
                        f"Message: {text}"
                    )
                }
            ]
        )
        print("[DASHBOARD LOG SENT]")
    except Exception as e:
        print("[DASHBOARD LOG ERROR]:", e)


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
    brand_name = ''
    ai_agent_id = None  # Initialize ai_agent_id
    lead_data_result = 0
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
            
            # cursor.execute("""
            #     SELECT 
            #         *,
            #         CASE 
            #             WHEN 
            #                 name IS NULL OR name = '' OR
            #                 email IS NULL OR email = '' OR
            #                 from_zip IS NULL OR from_zip = '' OR
            #                 to_zip IS NULL OR to_zip = '' OR
            #                 move_date IS NULL OR move_date = '' OR
            #                 move_size IS NULL OR move_size = ''
            #             THEN 0
            #             ELSE 1
            #         END AS is_complete
            #     FROM leads
            #     WHERE phone = %s
            #     ORDER BY lead_id DESC
            #     LIMIT 1
            # """, (from_number,))


            cursor.execute("""
                SELECT 
                    *,
                    CASE 
                        WHEN 
                            name IS NULL OR name = '' OR
                            email IS NULL OR email = ''
                        THEN 0
                        ELSE 1
                    END AS is_complete
                FROM leads
                WHERE phone = %s
                ORDER BY lead_id DESC
                LIMIT 1
            """, (from_number,))
            lead_data_res = cursor.fetchone()
            lead_data_result = lead_data_res['is_complete']
            print('adasdasdasdasd',lead_data_result)
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
                        lead_user_name = lead_data.get('name')
                        brand_name = f"{ai_agent_name}"
                        audio_message = f"Hi {lead_user_name}, I'm Calling from {ai_agent_name}. how are you?"
                    else:
                        brand_name = f"{ai_agent_name}"
                        audio_message = f"HI {ai_agent_name}. I got your lead from our agency. Are you looking for a move from somewhere?"
                else:
                        brand_name = f"{ai_agent_name}"
                        audio_message = f"Hi, I'm from {ai_agent_name}. how are you?"
                       
                        
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
    lead_type = lead_data['type'] if lead_data else 'outbound'
    site = lead_data['site'] if lead_data else 'PM'
    server = lead_data['server'] if lead_data else 'Stag'
    print(f"agent_id: {ai_agent_id if ai_agent_id else 'N/A'}")
    print(f"t_lead_id: {t_lead_id if t_lead_id else 'N/A'}")
    print(f"lead_type: {lead_type if lead_type else 'N/A'}")
    print(f"lead_data_result: {lead_data_result if lead_data_result else 'N/A'}")
    
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
    f"&amp;brand_name={quote(brand_name)}"
    f"&amp;ai_agent_id={ai_agent_id}"
    f"&amp;lead_timezone={lead_timezone}"
    f"&amp;site={site}"
    f"&amp;server={server}"
    f"&amp;lead_type={lead_type}"
    f"&amp;lead_data_result={lead_data_result}"
    )              
    print('ws-url',ws_url)
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
            conn = get_db_connection()
            if conn:
                try:
                    cursor = conn.cursor(dictionary=True, buffered=True)
                    cursor.execute("""
                        SELECT * FROM `leads` 
                        WHERE t_lead_id = %s 
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
                if lead_data and lead_data.get('phone') == "6025298353":
                    url = "https://snapit:mysnapit22@zapstage.snapit.software/api/calltransfertest"
                else:
                    url = "https://zapprod:zap2024@zap.snapit.software/api/calltransfertest"
            else:
                    print("to_number is not 12176186806")
                    if lead_data and lead_data.get('phone') in ("6025298353", "6263216095"):
                        url = "https://snapit:mysnapit22@stage.linkup.software/api/calltransfertest"
                    else:
                        url = "https://linkup:newlink_up34@linkup.software/api/calltransfertest"
            
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

# ===============================================================
# MAIN MEDIA STREAM HANDLER
# ===============================================================
@app.websocket('/media-stream')
async def handle_message():
    print('client connected')
    plivo_ws = websocket
    audio_message = websocket.args.get('audio_message', "Hi this is verse How can i help you?")
    ai_agent_name = websocket.args.get('ai_agent_name', 'AI Agent')
    brand_name = websocket.args.get('brand_name', 'Jason')
    call_uuid = websocket.args.get('CallUUID', 'unknown')
    voice_name = websocket.args.get('voice_name', 'alloy')
    ai_agent_id = websocket.args.get('ai_agent_id')  # Get ai_agent_id from URL params
    lead_id = websocket.args.get('lead_id', 'unknown')
    t_lead_id = websocket.args.get('t_lead_id', 'unknown')
    lead_timezone = websocket.args.get('lead_timezone', 'unknown')
    lead_phone = websocket.args.get('lead_phone', 'unknown')
    site = websocket.args.get('site', 'unknown')
    server = websocket.args.get('server', 'unknown')
    lead_type = websocket.args.get('lead_type', 'unknown')
    lead_data_result = websocket.args.get('lead_data_result', 'unknown')
    print('lead_id', lead_id)
    print('lead_data_result', lead_data_result)
    print('audio_message', audio_message)
    print('voice_name', voice_name)
    print('ai_agent_id', ai_agent_id)
    print('lead_timezone', lead_timezone)
    print('ai_agent_name', ai_agent_name)
    print('lead_phone', lead_phone)
    print('lead_type', lead_type)
    # Initialize conversation state with lock for active_response
    conversation_state = {
        'in_ai_response': False,
        'user_partial':'',
        'current_ai_text': '',
        'lead_id': lead_id,
        't_lead_id': t_lead_id,
        'site': site,
        'server': server,
        'call_uuid': call_uuid,
        'from_number': websocket.args.get('From', ''),
        'to_number': websocket.args.get('To', ''),
        'lead_phone': lead_phone,
        'active_response': False,  # Track if there's an active response to cancel
        'response_items': {},  # Store response items by ID
        'pending_language_reminder': False,  # Flag to send language reminder after current response
        'ai_transcript': '',
        'session_ready': False,
        '_audio_queue': [],  # if you queue audio until session is ready
    }


    prompt_text = ''  # Default to system message
    if ai_agent_id and lead_data_result == '1':
        conn = get_db_connection()
        if conn:
            try:
                cursor = conn.cursor(dictionary=True, buffered=True)
                # Fetch the active prompt for this ai_agent
                cursor.execute("SELECT * FROM ai_agent_prompts WHERE ai_agent_id = %s and is_active = 1", (ai_agent_id,))
                ai_agent_prompt = cursor.fetchone()
                if ai_agent_prompt:
                    prompt_text = ai_agent_prompt.get('prompt_text') or ai_agent_prompt.get('prompt') or ''
                    print("Loaded prompt_text (before replacements):", repr(prompt_text))
                    # If we have lead_id, fetch lead data and replace placeholders
                    if lead_id and lead_id != 'unknown':
                        try:
                            lead_id_int = int(lead_id)
                            cursor.execute("SELECT * FROM leads WHERE lead_id = %s", (lead_id_int,))
                            lead_data = cursor.fetchone()
                            if lead_data:
                                prompt_text = prompt_text.replace("[brand_name]", brand_name)
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
    print(f"prompt_text (after replacements): {repr(prompt_to_use)}")

    url = "wss://api.openai.com/v1/realtime?model=gpt-4o-realtime-preview-2024-10-01"
    #url = "wss://api.openai.com/v1/realtime?model=gpt-realtime"
    headers = {
        "Authorization": f"Bearer {OPENAI_API_KEY}",
        "OpenAI-Beta": "realtime=v1",
    }

    try:
        async with websockets.connect(url, extra_headers=headers) as openai_ws:
            print('connected to the OpenAI Realtime API')

            await send_Session_update(openai_ws,prompt_to_use,lead_type,lead_data_result)

             # Send the specific audio_message as initial prompt
            initial_prompt = {
                "type": "response.create",
                "response": {
                    "modalities": ["audio", "text"],
                    "temperature": 0.8,
                    "instructions": (
                        f"Start with this exact phrase: '{audio_message}' "
                        "IMPORTANT: Always complete your sentences and thoughts. Never stop speaking in the middle of a sentence or phrase.\n\n"
                    )
                }
            }
            # Send create request but DO NOT set active_response = True here.
            # Wait for 'response.created' event from server to set the flag.
            await openai_ws.send(json.dumps(initial_prompt))

            receive_task = asyncio.create_task(receive_from_plivo(plivo_ws, openai_ws))

            async for message in openai_ws:
                await receive_from_openai(message, plivo_ws, openai_ws, conversation_state)

            await receive_task

    except asyncio.CancelledError:
        print('client disconnected')
    except websockets.ConnectionClosed:
        print("Connection closed by OpenAI server")
    except Exception as e:
        print(f"Error during OpenAI websocket communication: {e}")

# ===============================================================
# RECEIVE AUDIO FROM PLIVO → SEND TO OPENAI
# ===============================================================
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


# ===============================================================
# RECEIVE FROM OPENAI → SEND AUDIO + HANDLE FUNCTIONS
# ===============================================================
async def receive_from_openai(message, plivo_ws, openai_ws, conversation_state):
    try:
        response = json.loads(message)
        evt_type = response.get("type", "")  # ALWAYS define evt_type early
        #print("OpenAI Event:", evt_type)

        # ------------------------
        # USER (incoming audio -> transcription)
        # ------------------------
        # partials (various names seen across API versions)
        if evt_type in (
            "input_audio_transcription.delta",
            "input_audio_transcription.partial",
            "conversation.item_input_audio_transcription.delta",
            "input_text.delta",
        ):
            text = response.get("delta") or response.get("text") or ""
            conversation_state["user_partial"] = conversation_state.get("user_partial", "") + text
            print("[USER PARTIAL]:", text)

        # final user transcription
        elif evt_type in (
            #"input_audio_buffer.committed",  # User finished speaking
            "input_audio_buffer.transcription.completed",  # Transcription ready
            "conversation.item.input_audio_transcription.completed",
        ):
            final_text = response.get("transcript") or response.get("text") or conversation_state.get("user_partial", "")
            final_text = final_text or ""
            print(f"\n[USER SAID]: {final_text}")
            if final_text!='':
                #await log_to_dashboard("user_message", final_text, conversation_state)
                await log_conversation(
                    conversation_state['lead_id'],
                    conversation_state['call_uuid'],
                    'user',
                    final_text
                )
            conversation_state["ai_transcript"] = conversation_state.get("ai_transcript", "") + f"USER: {final_text}\n"
            conversation_state["user_partial"] = ""  # reset

        # ------------------------
        # AI audio transcript (what the AI spoke) — partials
        # ------------------------
        elif evt_type == "response.audio_transcript.delta":
            text = response.get("delta") or ""
            conversation_state["current_ai_text"] = conversation_state.get("current_ai_text", "") + text
            #print("[AI PARTIAL]:", text)

        # AI audio transcript final
        elif evt_type == "response.audio_transcript.done":
            full = conversation_state.get("current_ai_text", "") or response.get("text", "") or ""
            print("\n[AI SAID]:", full)
            #await log_to_dashboard("ai_message", full, conversation_state)
            await log_conversation(
                    conversation_state['lead_id'],
                    conversation_state['call_uuid'],
                    'assistant',
                    full
                )
            conversation_state["ai_transcript"] = conversation_state.get("ai_transcript", "") + f"AI: {full}\n"
            conversation_state["current_ai_text"] = ""  # reset

        # ------------------------
        # AI audio frames (play audio to Plivo)
        # ------------------------
        elif evt_type == "response.audio.delta":
            # some responses use 'delta' (base64); make sure to handle accordingly
            delta_b64 = response.get("delta") or response.get("audio") or ""
            if delta_b64:
                audio_delta = {
                    "event": "playAudio",
                    "media": {
                        "contentType": "audio/x-mulaw",
                        "sampleRate": 8000,
                        "payload": base64.b64encode(base64.b64decode(delta_b64)).decode("utf-8")
                    }
                }
                await plivo_ws.send(json.dumps(audio_delta))

        elif evt_type == "response.audio.done":
            # optional: notify plivo that audio finished (if needed)
            print("[AI AUDIO DONE]")

        # ------------------------
        # Function call completed by model (your hosted prompt functions)
        # ------------------------
        elif evt_type == "response.function_call_arguments.done":
            fn = response.get("name")
            args_raw = response.get("arguments", "{}")
            try:
                args = json.loads(args_raw)
            except Exception:
                args = {}
            item_id = response.get("item_id")
            call_id = response.get("call_id")
            print("\n=== FUNCTION CALL RECEIVED ===", fn, args)
            conn = get_db_connection()
            cursor = conn.cursor()
            #print(f'Received function call response: {response}')
            args = json.loads(response['arguments'])
            print(f'Received custom tool response: {args}')
            cursor.execute("""
                INSERT INTO function_responses (lead_id, function_response)
                VALUES (%s, %s)
            """, (conversation_state['lead_id'], json.dumps(args)))
            conn.commit()
            print(f"Successfully stored function response for lead_id: {conversation_state['lead_id']}")
            if fn == "assign_customer_disposition":
                await handle_assign_disposition(openai_ws, args, item_id, call_id, conversation_state)
            elif fn == "update_lead":
                await update_or_add_lead_details(openai_ws, args, item_id, call_id, conversation_state)
            elif fn == "lookup_zip_options":
                await lookup_zip_options(openai_ws, args, item_id, call_id, conversation_state)

        # ------------------------
        # Session updated & errors
        # ------------------------
        elif evt_type == "session.updated":
            # print full session info when debugging; comment out in prod if noisy
            #print("[SESSION UPDATED]:", json.dumps(response.get("session", {})))
            print("\n=== SESSION UPDATED ===")
            # You should verify here that input_audio_transcription (or equivalent) is present:
            # print("Session keys:", list(response.get("session", {}).keys()))

        elif evt_type == "error":
            print("[OPENAI ERROR EVENT]:", json.dumps(response, indent=2))

        # ------------------------
        # When user starts speaking, cancel current AI response (you already used this)
        # ------------------------
        elif evt_type == "input_audio_buffer.speech_started":
            print("Speech started → cancel response")
            try:
                await plivo_ws.send(json.dumps({
                    "event": "clearAudio",
                    "stream_id": plivo_ws.stream_id
                }))
            except Exception:
                pass
            await openai_ws.send(json.dumps({"type": "response.cancel"}))

        # ------------------------
        # Other events: log for debugging
        # ------------------------
        else:
            # This will show you the full JSON for unknown event types so you can adapt the handler
            #print("Unhandled OpenAI event (full):", json.dumps(response))
            print("Unhandled OpenAI event (full):")

    except Exception as e:
        print("Error processing OpenAI message:", e)
        import traceback
        print(traceback.format_exc())


# ===============================================================
# HANDLE FUNCTION: assign_customer_disposition
# ===============================================================

async def handle_assign_disposition(openai_ws, args, item_id, call_id,conversation_state):
    print("\n=== Saving Disposition ===")
    print(args)
    ai_greeting_instruction = ''
    transfer_result = None
    follow_up_time = args.get("followup_time")
    if(args.get("disposition") != None):
        if(args.get("disposition") == 'Live Transfer'):
            transfer_result = await transfer_call(conversation_state['lead_id'],1,conversation_state['site'],conversation_state['server'])
        elif(args.get("disposition") == 'Truck Rental'):

            conn = get_db_connection()
            if conn:
                cursor = conn.cursor(dictionary=True, buffered=True)
                cursor.execute("SELECT * FROM lead_call_contact_details WHERE lead_id = %s AND call_type = 'truck_rental_transfer'", (conversation_state['lead_id'],))
                lead_truck_rental_data = cursor.fetchone()
                cursor.close()
                conn.close()
            else:
                raise Exception("Failed to get database connection")

            if lead_truck_rental_data:
                transfer_result = await transfer_call(conversation_state['lead_id'],2,conversation_state['site'],conversation_state['server'])
            else:
                await dispostion_status_update(conversation_state['lead_id'], args.get("disposition"),follow_up_time)
        
        
        
        else:
            await dispostion_status_update(conversation_state['lead_id'], args.get("disposition"),follow_up_time)
            ai_greeting_instruction = "I've saved the disposition. Is there anything else you'd like to do?"
    
    # Simulate DB save here
    # You can replace with real MySQL insert
    # 🔍 If transfer_result exists → check for API FAILURE
    if transfer_result:
        parsed = ensure_dict(transfer_result)
        print('tresult',parsed)
        try:
            status = parsed.get("status")
            api_error_message = parsed.get("error") or "Transfer failed."
            print('status',status)
            if status == "FAILURE":
                ai_greeting_instruction = api_error_message
        except Exception as e:
            print("Transfer result parsing error:", e)

    # If no FAILURE, continue with normal flow
    saved_output = {
        "status": "saved",
        "disposition": args.get("disposition"),
        "customer_response": args.get("customer_response"),
        "followup_time": args.get("followup_time"),
        "notes": args.get("notes"),
    }

    # 1️⃣ Send function output back to OpenAI
    await openai_ws.send(json.dumps({
        "type": "conversation.item.create",
        "item": {
            "id": item_id,
            "type": "function_call_output",
            "call_id": call_id,
            "output": json.dumps(saved_output)
        }
    }))

    # 2️⃣ Tell the model to speak confirmation
    await openai_ws.send(json.dumps({
        "type": "response.create",
        "response": {
            "modalities": ["audio", "text"],
            "instructions": ai_greeting_instruction
        }
    }))


async def lookup_zip_options(openai_ws, args, item_id, call_id, conversation_state):
    city = args.get("city")
    state = args.get("state")

    # Ask OpenAI to directly generate ZIP suggestions
    prompt = f"Suggest valid ZIP codes for {city}, {state}. Return only JSON: {{\"zip_codes\": [...]}}"
    
    await openai_ws.send(json.dumps({
        "type": "response.create",
        "response": {
            "instructions": prompt
        }
    }))


async def update_or_add_lead_details(openai_ws,args,item_id, call_id,conversation_state):
    try:
        print("\n=== Updating Lead Details ===")
        print(args)
        print(conversation_state)
        api_update_data = {}
        update_data = {}
        
        # Add lead_id to update_data
        api_update_data['lead_id'] = conversation_state['t_lead_id']
        lead_id = conversation_state.get('lead_id')
         # Map collected_facts to your database columns
        if args.get('name', ''):
            update_data['name'] = args['name']
            api_update_data['name'] = args['name']
        if args.get('phone', ''):
            update_data['phone'] = args['phone']
            api_update_data['phone'] = args['phone']
        if args.get('email', ''):
            update_data['email'] = args['email']
            api_update_data['email'] = args['email']
        if args.get('from_city'):
            update_data['from_city'] = args['from_city']
            api_update_data['from_city'] = args['from_city']
        if args.get('from_state'):
            update_data['from_state'] = args['from_state']
            api_update_data['from_state'] = args['from_state']
        if args.get('from_zip'):
            update_data['from_zip'] = args['from_zip']
            api_update_data['from_zipcode'] = args['from_zip']
        if args.get('to_city'):
            update_data['to_city'] = args['to_city']
            api_update_data['to_city'] = args['to_city']
        if args.get('to_state'):
            update_data['to_state'] = args['to_state']
            api_update_data['to_state'] = args['to_state']
        if args.get('to_zip'):
            update_data['to_zip'] = args['to_zip']
            api_update_data['to_zipcode'] = args['to_zip']
        if args.get('move_size'):
            move_size_lower = args['move_size'].lower()
        
            if re.search(r'\bstudio(\s*(apartment|room))?\b', move_size_lower):
                update_data['move_size'] = 1
                api_update_data['move_size_id'] = 1
            elif re.search(r'\b(1\s*bed(room)?s?|one\s*bed(room)?s?|1\s*br|1\s*bhk)\b', move_size_lower):
                update_data['move_size'] = 2
                api_update_data['move_size_id'] = 2
            elif re.search(r'\b(2\s*bed(room)?s?|two\s*bed(room)?s?|2\s*br|2\s*bhk)\b', move_size_lower):
                update_data['move_size'] = 3
                api_update_data['move_size_id'] = 3
            elif re.search(r'\b(3\s*bed(room)?s?|three\s*bed(room)?s?|3\s*br|3\s*bhk)\b', move_size_lower):
                update_data['move_size'] = 4
                api_update_data['move_size_id'] = 4
            elif re.search(r'\b(4\s*bed(room)?s?|four\s*bed(room)?s?|4\s*br|4\s*bhk)\b', move_size_lower):
                update_data['move_size'] = 5
                api_update_data['move_size_id'] = 5
            elif re.search(r'\b(5\s*\+|5\s*bed(room)?s?|five\s*bed(room)?s?|5\s*br|5\s*bhk)\b', move_size_lower):
                update_data['move_size'] = 6
                api_update_data['move_size_id'] = 6
            else:
                update_data['move_size'] = args['move_size']
                api_update_data['move_size_id'] = args['move_size']
        if args.get('move_date'):
            original_date = args['move_date']
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
        
        print('api_update_data',api_update_data)
        api_success = await update_lead_to_external_api(api_update_data,conversation_state['call_uuid'], conversation_state['lead_id'],conversation_state['site'],conversation_state['server'])
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

            await openai_ws.send(json.dumps({
                "type": "conversation.item.create",
                "item": {
                    "id": item_id,
                    "type": "function_call_output",
                    "call_id": call_id,
                    "output": json.dumps(args)
                }
            }))

            await openai_ws.send(json.dumps({
                "type": "response.create",
                "response": {
                    "modalities": ["audio", "text"],
                    "instructions": (
                        "Continue the conversation with the caller incorporating the updated lead details: "
                        + json.dumps(args)
                    )
                }
            }))
            print("[LEAD_UPDATE] Sent function output and requested model response.")
            return True
        else:
            print(f"[LEAD_UPDATE] No rows updated - lead_id {lead_id} may not exist")
            return False
        
    except Exception as e:
        print(f"[LEAD_UPDATE] Error updating lead {lead_id}: {str(e)}")
        import traceback
        print(f"[DEBUG] Full traceback: {traceback.format_exc()}")
        if 'conn' in locals() and conn:
            conn.close()
        return False


async def update_lead_to_external_api(api_update_data, call_u_id, lead_id, site, server):
    """
    Sends api_update_data to external updateailead endpoint and if the response
    contains live_transfer / truck_rental, delete existing rows for the lead & call_types
    then insert the new rows into lead_call_contact_details.

    Returns True on API success (200 handled), False otherwise.
    """
    print("[TRANSFER] Updating lead to external API", api_update_data)
    try:
        # choose URL
        if site == "ZAP":
            if server == "Prod":
                url = "https://zapprod:zap2024@zap.snapit.software/api/updateailead"
            else:
                url = "https://snapit:mysnapit22@zapstage.snapit.software/api/updateailead"
        else:
            if server == "Prod":
                url = "https://linkup:newlink_up34@linkup.software/api/updateailead"
            else:
                url = "https://snapit:mysnapit22@stage.linkup.software/api/updateailead"

        print(f"[TRANSFER] Using URL: {url}")

        async with aiohttp.ClientSession() as session:
            # small delay (kept from your original)
            await asyncio.sleep(2)
            async with session.post(
                url,
                headers={'Content-Type': 'application/json'},
                json=api_update_data,
                timeout=aiohttp.ClientTimeout(total=30)
            ) as resp:
                response_text = await resp.text()
                if resp.status != 200:
                    print(f"[TRANSFER] API call failed with status {resp.status}: {response_text}")
                    return False

                print(f"[TRANSFER] API call successful: {response_text}")

                # parse JSON safely
                try:
                    payload = json.loads(response_text)
                except Exception as e:
                    print(f"[TRANSFER] Failed to parse JSON response: {e}")
                    return True  # external API OK; nothing to save locally

                data = payload.get("data") or {}
                # Use provided lead_id (caller passes this)
                ret_lead_id = lead_id

                # helper to normalize phone numbers to digits only
                def normalize_phone(p):
                    if not p:
                        return p
                    digits = re.sub(r"\D+", "", str(p))
                    return digits if digits else p

                # collect contact entries (keys from your API are "live_transfer" and "truck_rental_transfer")
                contacts_to_insert = []
                call_types_seen = set()

                # NOTE: iterate explicit (api_node_key, call_type_value) pairs
                pairs = (
                    ("live_transfer", "live_transfer"),
                    ("truck_rental_transfer", "truck_rental_transfer"),
                )

                for key, call_type in pairs:
                    node = data.get(key)
                    if node and isinstance(node, dict):
                        name = node.get("mover_name") or node.get("name") or None
                        phone = node.get("mover_phone") or node.get("phone") or None
                        if not (name or phone):
                            print(f"[TRANSFER] Skipping {key} — no name/phone present")
                            continue
                        call_types_seen.add(call_type)
                        contacts_to_insert.append({
                            "lead_id": int(ret_lead_id) if (ret_lead_id is not None and str(ret_lead_id).isdigit()) else (ret_lead_id or 0),
                            "calluuid": call_u_id or api_update_data.get('calluuid') or '',
                            "call_type": call_type,
                            "name": name,
                            "phone": normalize_phone(phone)
                        })

                if not contacts_to_insert:
                    print("[TRANSFER] No live_transfer/truck_rental data to save.")
                    return True

                # Insert contacts into DB, deleting existing ones for this lead & call_types first (if lead_id provided)
                conn = get_db_connection()
                if not conn:
                    print("[TRANSFER] Failed to get DB connection to insert contacts")
                    return True  # external API succeeded; return True but log DB issue

                try:
                    cur = conn.cursor()
                    try:
                        # If we have a valid lead_id (numeric), perform delete for the seen call_types
                        if ret_lead_id and str(ret_lead_id).isdigit() and call_types_seen:
                            lead_id_int = int(ret_lead_id)
                            call_types_list = list(call_types_seen)
                            placeholders = ", ".join(["%s"] * len(call_types_list))
                            delete_sql = f"""
                                DELETE FROM lead_call_contact_details
                                WHERE lead_id = %s AND call_type IN ({placeholders})
                            """
                            params = [lead_id_int] + call_types_list
                            print(f"[TRANSFER] Deleting existing contacts for lead_id={lead_id_int} call_types={call_types_list}")
                            cur.execute(delete_sql, params)
                            print(f"[TRANSFER] Deleted {cur.rowcount} existing contact(s)")
                        else:
                            print("[TRANSFER] No valid lead_id or no call_types to delete; skipping delete step.")
                    except Exception as e:
                        print(f"[TRANSFER] Error during delete step: {e}")
                        # proceed to inserts anyway

                    # Insert new contacts
                    insert_sql = """
                        INSERT INTO lead_call_contact_details
                            (lead_id, calluuid, call_type, name, phone)
                        VALUES (%s, %s, %s, %s, %s)
                    """
                    insert_values = [
                        (c['lead_id'], c['calluuid'], c['call_type'], c['name'], c['phone'])
                        for c in contacts_to_insert
                    ]
                    cur.executemany(insert_sql, insert_values)
                    conn.commit()
                    print(f"[TRANSFER] Inserted {cur.rowcount} contact(s) into lead_call_contact_details for lead {ret_lead_id}")
                    cur.close()
                except Exception as e:
                    print(f"[TRANSFER] Failed to delete/insert contact details: {e}")
                    try:
                        conn.rollback()
                    except Exception:
                        pass
                finally:
                    try:
                        if conn.is_connected():
                            conn.close()
                    except Exception:
                        pass

                return True

    except Exception as e:
        print(f"[TRANSFER] Exception during update_lead_to_external_api: {e}")
        return False


async def transfer_call(lead_id,transfer_type,site,server):
        try:
            print(f"[TRANSFER] Starting call transfer for lead_id: {lead_id}")
            
            # Fetch lead data from database
            conn = get_db_connection()
            if conn:
                cursor = conn.cursor(dictionary=True, buffered=True)
                cursor.execute("SELECT * FROM leads WHERE lead_id = %s", (lead_id,))
                lead_data = cursor.fetchone()
                if transfer_type == 2:
                    cursor.execute("SELECT * FROM lead_call_contact_details WHERE lead_id = %s AND call_type = 'truck_rental_transfer'", (lead_id,))
                    lead_truck_rental_data = cursor.fetchone()
                else:
                    lead_truck_rental_data = None
                cursor.close()
                conn.close()
            else:
                raise Exception("Failed to get database connection")
            
            if not lead_data:
                raise Exception(f"Lead not found for lead_id: {lead_id}")
            
            print(f"[TRANSFER] Lead Data: {lead_data['phone']}")
            
            # Prepare the payload
            payload = {
            'id': lead_data.get('t_call_id'),
            'action': 1,
            'usaBusinessCheck': 1 if transfer_type == 2 else 0,
            'type': 1,
            'review_call': lead_data.get('review_call',0),  # defaults to 0 if None or missing
            'accept_call': 0,
            'rep_id': lead_data.get('t_rep_id'),
            'logic_check': 1,
            'lead_id': lead_data.get('t_lead_id'),
            'categoryId': 1,
            'buffer_id_arr': '',
            'campaignId': lead_data.get('campaign_id_truck') if transfer_type == 2 else lead_data.get('campaign_id'),
            'campaignScore': lead_data.get('campaign_score'),
            'campaignNumber': lead_truck_rental_data.get('phone') if transfer_type == 2 else lead_data.get('mover_phone'),
            'campaignPayout': lead_data.get('campaign_payout_truck') if transfer_type == 2 else lead_data.get('campaign_payout'),
            }
            
            print(f"[TRANSFER] Payload: {payload}")
            # Determine URL based on phone number

            if site == "ZAP":
                if server == "Prod":
                    url = "https://zapprod:zap2024@zap.snapit.software/api/calltransfertest"
                else:
                    url = "https://snapit:mysnapit22@zapstage.snapit.software/api/calltransfertest"
            else:
                if server == "Prod":
                    url = "https://linkup:newlink_up34@linkup.software/api/calltransfertest"
                else:
                    url = "https://snapit:mysnapit22@stage.linkup.software/api/calltransfertest"
            
            print(f"[TRANSFER] Using URL: {url}")
            
            # Make the API call
            async with aiohttp.ClientSession() as session:
                # ✅ Add 3-second delay before making API call
                await asyncio.sleep(2)
                async with session.post(
                    url,
                    headers={'Content-Type': 'application/json'},
                    data=json.dumps(payload)
                ) as resp:
                    if resp.status == 200:
                        response_text = await resp.text()
                        print(f"[TRANSFER] API call successful: {response_text}")
                        return response_text
                    else:
                        response_text = await resp.text()
                        raise Exception(f"API call failed with status {resp.status}: {response_text}")
        
        except Exception as e:
            print(f"[TRANSFER] Error: {e}")



async def dispostion_status_update(lead_id, disposition_val,follow_up_time):
    try:

        if disposition_val == 'DNC':
            disposition = 2
        elif disposition_val == 'Not Interested':
            disposition = 3
        elif disposition_val == 'Follow Up':
            disposition = 4
        elif disposition_val == 'No Buyer':
            disposition = 5
        elif disposition_val == 'Voicemail':
            disposition = 6
        elif disposition_val == 'Wrong Phone':
            disposition = 7
        elif disposition_val == 'Booked':
            disposition = 8
        elif disposition_val == 'Only Call':
            disposition = 9
        elif disposition_val == 'Booked with Us':
            disposition = 10
        elif disposition_val == 'Booked with PODs':
            disposition = 11
        elif disposition_val == 'Booked with Truck Rental':
            disposition = 12
        elif disposition_val == 'Truck Rental':
            disposition = 13
        elif disposition_val == 'IB Pickup':
            disposition = 14
        elif disposition_val == 'No Answer':
            disposition = 15
        elif disposition_val == 'Business Relay':
            disposition = 16
        elif disposition_val == 'Agent Transfer':
            disposition = 18 
        else:
            disposition = 17

        params = {
                "lead_id": lead_id,
                "disposition": disposition
            }

        if disposition == 4:
            params["followupdatetime"] = follow_up_time
            print(f"[DISPOSITION] Lead {lead_id} disposition updated to {disposition}")
        # Build the URL with proper encoding
        query_string = urlencode(params, quote_via=quote)
        redirect_url = f"http://54.176.128.91/disposition_route?{query_string}"
        print(f"[DISPOSITION] Redirect URL: {redirect_url}")
        response = requests.post(redirect_url)
        print(f"[DEBUG] Redirect URL: {response}")
        print(f"[DISPOSITION] Lead {lead_id} disposition updated to {disposition}")
    except Exception as e:
        print(f"[DISPOSITION] Error updating lead disposition: {e}")


# ===============================================================
# SEND SESSION UPDATE (HOSTED PROMPT ONLY)
# ===============================================================
async def send_Session_update(openai_ws,prompt_to_use,lead_type,lead_data_result):

    if lead_data_result == '1':
        print("outbound")
        prompt_obj = {
            "id": "pmpt_69175111ddb88194b4a88fc70e6573780dfc117225380ded"
        }
    else:
        print("inbound")
        prompt_obj = {
            "id": "pmpt_691652392c1c8193a09ec47025d82ac305f13270ca49da07"
        }
    
    # Force English responses
    session_update = {
        "type": "session.update",
        "session": {
            "prompt": prompt_obj,
            "instructions": prompt_to_use,
            "input_audio_format": "g711_ulaw",
            "output_audio_format": "g711_ulaw",
            "input_audio_transcription": {"model": "whisper-1"},  # Enable transcription
            "modalities": ["text", "audio"],
            "turn_detection": {
                "type": "server_vad",
                "threshold": 0.5,
                "prefix_padding_ms": 300,
                "silence_duration_ms": 500
            }
        }
    }
    await openai_ws.send(json.dumps(session_update))
    print("Session update sent.")

# ===============================================================
# MAIN START
# ===============================================================
if __name__ == "__main__":
    initialize_database()
    print("Running server...")
    app.run(port=PORT)
