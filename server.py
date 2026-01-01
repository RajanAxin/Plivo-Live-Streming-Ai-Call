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
from datetime import datetime, timedelta
import pytz
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


def segment_speakers_new(transcript_text: str):
    """Ask GPT to analyze entire transcript and return final disposition only."""
    response = openai.chat.completions.create(
        model="gpt-4o-mini",
        messages=[
            {"role": "system", "content": """You are a call disposition classifier for a moving company. Analyze the conversation and select the exact matching disposition based on these RULES:

        1. "booked with you/your team/scheduled with you" → Booked with Us
        2. "already booked/hired movers/scheduled elsewhere/we're set" → Booked  
        3. "booked u-haul/penske/ryder/budget truck/rented truck" → Booked with Truck Rental
        4. "booked pods/container/PODS/pack rat/1-800-packrat" → Booked with PODs
        5. "not interested/don't need/not looking" → Not Interested
        6. "wrong number/wrong person/not me/no idea" → Wrong Phone
        7. "do not call/stop calling/remove me" → DNC
        8. "no buyer available/buyer not available" → No Buyer
        9. Human agent requested between 6PM-8AM EST → Next 8AM EST Followup
        10. "call me back/call tomorrow/next week/next month" → Follow Up
        11. "leave message/voicemail" → Voice Message
        12. "no answer/didn't pick up/disconnected" → No Answer
        13. "business line/company phone/office number" → Business Relay
        14. Transfer initiated to mover/truck rental → Transfer Initiated
        15. Call never connected → Not Connected
        16. Normal moving conversation → Connected - Information Gathering

        For time checks: If human agent is requested and current time is 6PM-8AM EST, use "Next 8AM EST Followup"
        For booking types: Be specific about which company they booked with
        For conversation state: If actively discussing moving details, use "Connected - Information Gathering"

        Select ONLY ONE disposition from these options:
        - Not Connected
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
        - No Answer
        - Business Relay
        - Next 8AM EST Followup
        - Transfer Initiated
        - Connected - Information Gathering"""},
            {"role": "user", "content": f"""
            Analyze the following call transcript and determine the correct disposition.
            Do NOT split into speakers. Use the entire transcript as-is.

            TRANSCRIPT:
            {transcript_text}

            CONTEXT: This is a moving company conversation. The AI agent collects customer info and helps with moving questions. The AI follows these specific rules:
            
            1. If customer says "booked with you" → Booked with Us
            2. If customer says "already booked" → Booked
            3. If customer mentions specific truck rental company → Booked with Truck Rental
            4. If customer mentions PODS/container → Booked with PODs
            5. If wrong number → Wrong Phone
            6. If human requested 6PM-8AM EST → Next 8AM EST Followup
            7. If callback requested → Follow Up
            8. If no buyer available → No Buyer
            9. If actively discussing move → Connected - Information Gathering
            10. If being transferred → Transfer Initiated
            
            CURRENT TIME CONTEXT: Assume EST timezone. If transcript mentions "human", "agent", "representative", or "customer service" request, check if it's between 6PM-8AM EST.
            
            IMPORTANT: Look for these EXACT phrases in the transcript:
            - "booked with you", "with your team" → Booked with Us
            - "booked already", "hired movers" → Booked
            - "u-haul", "penske", "ryder" → Booked with Truck Rental
            - "pods", "container", "pack rat" → Booked with PODs
            - "wrong number", "not me" → Wrong Phone
            - "do not call" → DNC
            - "call me back" → Follow Up
            - "no buyer" → No Buyer
            
            Output ONLY valid JSON in the following format:
            {{
                "disposition": "<one_of_the_above_dispositions>",
                "transcript_text": "{transcript_text}"
            }}
            
            Select the MOST SPECIFIC match based on the exact phrases in the transcript.
            """}
        ],
        response_format={"type": "json_object"}  # ensure valid JSON
    )
    return response.choices[0].message.content



def segment_speakers(transcript_text: str):
    """Ask GPT to analyze entire transcript and return final disposition only."""
    response = openai.chat.completions.create(
        model="gpt-4o-mini",
        messages=[
            {"role": "system", "content": "You are a call disposition classifier."},
            {"role": "user", "content": f"""
            Analyze the following call transcript and determine the correct disposition.
            Do NOT split into speakers. Use the entire transcript as-is.

            Transcript:
            {transcript_text}

            Possible dispositions:
            - Not Connected
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
            - No Answer

            Output ONLY valid JSON in the following format:
            {{
                "disposition": "<one_of_the_above>",
                "transcript_text": "{transcript_text}"
            }}
            """}
        ],
        response_format={"type": "json_object"}  # ensure valid JSON
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
    brand_id = brand_id
    call_uuid = lead_data['calluuid'] if lead_data else 0
    lead_timezone = lead_data['t_timezone'] if lead_data else 0
    lead_phone = lead_data['phone'] if lead_data else 0
    t_lead_id = lead_data['t_lead_id'] if lead_data else 0
    t_call_id = lead_data['t_call_id'] if lead_data else 0
    lead_type = lead_data['type'] if lead_data else 'outbound'
    site = lead_data['site'] if lead_data else 'PM'
    server = lead_data['server'] if lead_data else 'Stag'
    lead_numbers_id = lead_data.get('lead_numbers_id')
    if lead_numbers_id is None:
        lead_numbers_id = 0
    print(f"agent_id: {ai_agent_id if ai_agent_id else 'N/A'}")
    print(f"brand_id: {brand_id if brand_id else 'N/A'}")
    print(f"t_lead_id: {t_lead_id if t_lead_id else 'N/A'}")
    print(f"t_call_id: {t_call_id if t_call_id else 'N/A'}")
    print(f"lead_type: {lead_type if lead_type else 'N/A'}")
    print(f"lead_numbers_id: {lead_numbers_id if lead_numbers_id else 'N/A'}")
    print(f"lead_data_result: {lead_data_result if lead_data_result else 'N/A'}")
    
    ws_url = (
    f"wss://{request.host}/media-stream?"
    f"audio_message={quote(audio_message)}"
    f"&amp;CallUUID={call_uuid}"
    f"&amp;From={from_number}"
    f"&amp;To={to_number}"
    f"&amp;lead_phone={lead_phone}"
    f"&amp;lead_id={lead_id}"
    f"&amp;brand_id={brand_id}"
    f"&amp;t_lead_id={t_lead_id}"
    f"&amp;t_call_id={t_call_id}"
    f"&amp;voice_name={voice_name}"
    f"&amp;ai_agent_name={quote(ai_agent_name)}"
    f"&amp;brand_name={quote(brand_name)}"
    f"&amp;ai_agent_id={ai_agent_id}"
    f"&amp;lead_timezone={lead_timezone}"
    f"&amp;site={site}"
    f"&amp;server={server}"
    f"&amp;lead_type={lead_type}"
    f"&amp;lead_numbers_id={lead_numbers_id}"
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
# SILENCE DETECTOR FUNCTION
# ===============================================================
async def monitor_silence(plivo_ws, conversation_state):
    """Monitors the conversation for silence and disconnects after 1 minute."""
    try:
        while True:
            # Check every 5 seconds to save resources
            await asyncio.sleep(5)
            # Get current time and last activity time from state
            now = asyncio.get_event_loop().time()
            last_activity = conversation_state.get('last_activity_time', 0)
            
            # Calculate seconds of silence
            silence_duration = now - last_activity

            # If silence exceeds 120 seconds, hang up
            if silence_duration > 120:
                print(f"[SILENCE DETECTOR] Silence detected for {int(silence_duration)} seconds. Hanging up call {conversation_state.get('call_uuid')}.")
                try:
                    # Send hangup command to Plivo
                    #await dispostion_status_update(conversation_state['lead_id'], "Voicemail", "")
                    await plivo_ws.send(json.dumps({"event": "hangup"}))
                except Exception as e:
                    print(f"[SILENCE DETECTOR] Error sending hangup: {e}")
                break  # Stop the detector loop
    except asyncio.CancelledError:
        print("[SILENCE DETECTOR] Task cancelled.")


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
    brand_id = websocket.args.get('brand_id', 'unknown')
    t_lead_id = websocket.args.get('t_lead_id', 'unknown')
    t_call_id = websocket.args.get('t_call_id', 'unknown')
    lead_timezone = websocket.args.get('lead_timezone', 'unknown')
    lead_phone = websocket.args.get('lead_phone', 'unknown')
    site = websocket.args.get('site', 'unknown')
    server = websocket.args.get('server', 'unknown')
    lead_type = websocket.args.get('lead_type', 'unknown')
    lead_numbers_id = websocket.args.get('lead_numbers_id', 'unknown')
    lead_data_result = websocket.args.get('lead_data_result', 'unknown')
    print('lead_id', lead_id)
    print('lead_numbers_id', lead_numbers_id)
    print('t_call_id', t_call_id)
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
        'lead_numbers_id': lead_numbers_id,
        't_lead_id': t_lead_id,
        't_call_id': t_call_id,
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
        '_audio_queue': [],  # if you queue audio until session is ready,
        'last_activity_time': asyncio.get_event_loop().time(),  # Track last activity time
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
                                    safe_value = "" if value is None else str(value)
                                    # Special handling for move_size placeholder
                                    if key == "move_size" and value:
                                        cursor.execute("SELECT move_size FROM mst_move_size WHERE move_size_id = %s", (value,))
                                        size_row = cursor.fetchone()
                                        if size_row:
                                            prompt_text = prompt_text.replace(placeholder, str(size_row["move_size"]))
                                        else:
                                            prompt_text = prompt_text.replace(placeholder, safe_value)  # fallback
                                    elif key == "lead_status" and value:
                                        prompt_text = prompt_text.replace("[lead_status]", safe_value)
                                    elif key == "payment_link" and value:
                                        prompt_text = prompt_text.replace("[payment_link]", safe_value)
                                    elif key == "invoice_link" and value:
                                        prompt_text = prompt_text.replace("[invoice_link]", safe_value)
                                    elif key == "inventory_link" and value:
                                        prompt_text = prompt_text.replace("[inventory_link]", safe_value)
                                    elif key == "booking_id" and value:
                                        prompt_text = prompt_text.replace("[booking_id]", safe_value)
                                    else:
                                        prompt_text = prompt_text.replace(placeholder, safe_value)
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

    #url = "wss://api.openai.com/v1/realtime?model=gpt-4o-realtime-preview-2024-10-01"
    url = "wss://api.openai.com/v1/realtime?model=gpt-4o-realtime-preview-2024-12-17"
    #url = "wss://api.openai.com/v1/realtime?model=gpt-realtime"
    headers = {
        "Authorization": f"Bearer {OPENAI_API_KEY}",
        "OpenAI-Beta": "realtime=v1",
    }

    try:
        async with websockets.connect(url, extra_headers=headers) as openai_ws:
            print('connected to the OpenAI Realtime API')

            await send_Session_update(openai_ws,prompt_to_use,brand_id,lead_data_result)

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

            # Initialize last activity time
            conversation_state['last_activity_time'] = asyncio.get_event_loop().time()

            # Helper to consume OpenAI messages as a coroutine for gathering
            async def consume_openai_messages():
                async for message in openai_ws:
                    await receive_from_openai(message, plivo_ws, openai_ws, conversation_state)

            # Create tasks for Plivo receiving, OpenAI consuming, and Silence monitoring
            receive_task = asyncio.create_task(receive_from_plivo(plivo_ws, openai_ws))
            openai_task = asyncio.create_task(consume_openai_messages())
            silence_task = asyncio.create_task(monitor_silence(plivo_ws, conversation_state))

            # Run all tasks together. If one fails (e.g. hangup), handle it.
            done, pending = await asyncio.wait(
                [receive_task, openai_task, silence_task],
                return_when=asyncio.FIRST_COMPLETED
            )

            # Cancel remaining tasks if one finishes (e.g. silence detector hangs up)
            for task in pending:
                task.cancel()

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
            # UPDATE: Reset timer when AI is speaking
            conversation_state['last_activity_time'] = asyncio.get_event_loop().time()
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
            elif fn == "set_call_disposition":
                await handle_ma_lead_set_call_disposition(openai_ws, args, item_id, call_id, conversation_state)
            elif fn == "update_lead":
                await update_or_add_lead_details(openai_ws, args, item_id, call_id, conversation_state)
            elif fn == "send_inventory_link":
                await send_inventory_link(openai_ws, args, item_id, call_id, conversation_state)
            elif fn == "send_payment_link":
                await send_payment_link(openai_ws, args, item_id, call_id, conversation_state)
            elif fn == "send_invoice_link":
                await send_invoice_link(openai_ws, args, item_id, call_id, conversation_state)
            elif fn == "add_lead_note":
                await add_lead_note(openai_ws, args, item_id, call_id, conversation_state)
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
            # UPDATE: Reset timer on user speech
            conversation_state['last_activity_time'] = asyncio.get_event_loop().time()
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

# Define this helper function at the module level, outside any other function
async def delayed_disposition_update(lead_id, disposition, follow_up_time):
    """Helper function to update disposition after a 14-second delay"""
    try:
        print(f"[DELAYED_DISPOSITION] Waiting 14 seconds before updating disposition...")
        await asyncio.sleep(14)
        print(f"[DELAYED_DISPOSITION] Updating disposition for lead {lead_id} to {disposition}")
        await dispostion_status_update(lead_id, disposition, follow_up_time)
        print(f"[DELAYED_DISPOSITION] Disposition update completed for lead {lead_id}")
    except Exception as e:
        print(f"[DELAYED_DISPOSITION] Error updating disposition: {e}")


async def log_call_action(lead_id, action_type, status, details, conversation_state=None, args=None):
    """
    Log actions to database using your exact pattern
    
    Parameters:
    - lead_id: The lead identifier
    - action_type: Type of action (e.g., 'DISPOSITION_UPDATE', 'TRANSFER_ATTEMPT')
    - status: Status of action (e.g., 'SUCCESS', 'FAILURE', 'INFO', 'WARNING')
    - details: Detailed information about the action
    - conversation_state: Optional conversation state for context
    - args: Optional function arguments for context
    """
    conn = get_db_connection()
    if conn:
        try:
            cursor = conn.cursor()
            
            # Prepare JSON data as strings
            conversation_data_str = json.dumps(conversation_state, default=str) if conversation_state else None
            function_args_str = json.dumps(args, default=str) if args else None
            timestamp = datetime.now().isoformat()
            
            insert_query = """
                INSERT INTO call_logs 
                (lead_id, action_type, status, details, timestamp, conversation_data, function_args)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
            """
            
            cursor.execute(
                insert_query, 
                (lead_id, action_type, status, details, timestamp, conversation_data_str, function_args_str)
            )
            conn.commit()
            
            # Optional: Log to console for debugging
            print(f"[LOG] Action logged for lead {lead_id}: {action_type} - {status}")
            
        except Exception as db_error:
            print(f"[DB ERROR] Failed to insert log for lead {lead_id}: {db_error}")
        finally:
            cursor.close()
            conn.close()
    else:
        print(f"[LOG ERROR] No database connection for lead {lead_id}")
        
        
async def handle_assign_disposition(openai_ws, args, item_id, call_id, conversation_state):
    print("\n=== Saving Disposition ===")
    print(args)
    ai_greeting_instruction = ''
    transfer_result = None
    follow_up_time = args.get("followup_time")
    
    # Log start of disposition assignment
    await log_call_action(
        conversation_state['lead_id'],
        'DISPOSITION_ASSIGNMENT_START',
        'INFO',
        f"Starting disposition assignment with args: {args}",
        conversation_state,
        args
    )

    if args.get("disposition") is not None:
        disposition = args.get("disposition")
        
        # Log disposition selection
        await log_call_action(
            conversation_state['lead_id'],
            'DISPOSITION_SELECTED',
            'INFO',
            f"User selected disposition: {disposition}",
            conversation_state
        )

        if disposition == 'Live Transfer':
            # Log start of live transfer process
            await log_call_action(
                conversation_state['lead_id'],
                'LIVE_TRANSFER_START',
                'INFO',
                f"Starting live transfer process. Company preference: {args.get('moving_company')}",
                conversation_state
            )
            
            check_individual_company_avaliabe = await check_individual_company_avaliability(
                conversation_state['lead_id'], 
                args.get("moving_company")
            )
            
            # Log individual company check result
            await log_call_action(
                conversation_state['lead_id'],
                'INDIVIDUAL_COMPANY_CHECK',
                'INFO' if check_individual_company_avaliabe else 'WARNING',
                f"Individual company availability check result: {check_individual_company_avaliabe}",
                conversation_state
            )
            
            if check_individual_company_avaliabe == None:
                check_company_avaliabe = await company_avaliability(conversation_state['lead_id'], 1)
                
                # Log general company availability check
                await log_call_action(
                    conversation_state['lead_id'],
                    'COMPANY_AVAILABILITY_CHECK',
                    'INFO' if check_company_avaliabe else 'WARNING',
                    f"General company availability (type 1) result: {check_company_avaliabe}",
                    conversation_state
                )
                
                print('asdasdasdasdasdasd',check_company_avaliabe)
                if check_company_avaliabe == None:
                    check_call_forward_company_avaliabe = await call_forward_company_avaliability(conversation_state['lead_id'])
                    
                    # Log call forward company availability check
                    await log_call_action(
                        conversation_state['lead_id'],
                        'CALL_FORWARD_COMPANY_CHECK',
                        'INFO' if check_call_forward_company_avaliabe else 'WARNING',
                        f"Call forward companies available: {check_call_forward_company_avaliabe}",
                        conversation_state
                    )
                    
                    if(check_call_forward_company_avaliabe == None):
                        ai_greeting_instruction = "This buyer not available at this moment and no other buyer available"
                        
                        # Log no buyers available
                        await log_call_action(
                            conversation_state['lead_id'],
                            'NO_BUYERS_AVAILABLE',
                            'WARNING',
                            "No moving companies available for live transfer",
                            conversation_state
                        )
                        
                        await ai_instract_guid(
                            openai_ws, ai_greeting_instruction, item_id, 
                            call_id, conversation_state, 'No Buyer', 
                            ai_greeting_instruction, follow_up_time, 1
                        )
                    else:
                        print('list of move com-',check_call_forward_company_avaliabe)
                        ai_greeting_instruction = f"We have the following moving companies available: {check_call_forward_company_avaliabe}. Please ask the user which company they want to talk to and after user choose a company call assign_customer_disposition function compalsory."
                        ai_instruct = f"I have these moving companies available for you: {check_call_forward_company_avaliabe}. Which company would you like to connect with?"
                        
                        # Log companies found, awaiting user choice
                        await log_call_action(
                            conversation_state['lead_id'],
                            'COMPANIES_FOUND_AWAITING_CHOICE',
                            'INFO',
                            f"Companies available for user choice: {check_call_forward_company_avaliabe}",
                            conversation_state
                        )
                        
                        await ai_instract_guid(
                            openai_ws, ai_greeting_instruction, item_id, call_id, 
                            conversation_state, 'Companies Found - Awaiting Choice', 
                            ai_instruct, follow_up_time, 0
                        )
                else:
                    ai_greeting_instruction = "Yes we have moving company avaliable"
                    
                    # Log company available for transfer
                    await log_call_action(
                        conversation_state['lead_id'],
                        'COMPANY_AVAILABLE_FOR_TRANSFER',
                        'INFO',
                        f"Company available for transfer: {check_company_avaliabe}",
                        conversation_state
                    )
                    
                    transfer_result = await transfer_call(
                        conversation_state['lead_id'], 1, 
                        conversation_state['site'], conversation_state['server']
                    )
                    
                    # Log transfer initiation
                    await log_call_action(
                        conversation_state['lead_id'],
                        'TRANSFER_INITIATED',
                        'INFO',
                        "Transfer call initiated for company type 1",
                        conversation_state
                    )
            else:
                print('user selected company detail')
                print(check_individual_company_avaliabe)
                ai_greeting_instruction = "User successfully selected a call forward company now please make a call"
                
                # Log user selected specific company
                await log_call_action(
                    conversation_state['lead_id'],
                    'USER_SELECTED_COMPANY',
                    'INFO',
                    f"User selected specific company: {check_individual_company_avaliabe}",
                    conversation_state
                )
                
                transfer_result = await call_forward_transfer_call(
                    conversation_state['lead_id'], check_individual_company_avaliabe, 
                    conversation_state['site'], conversation_state['server']
                )
                
                # Log call forward transfer initiation
                await log_call_action(
                    conversation_state['lead_id'],
                    'CALL_FORWARD_TRANSFER_INITIATED',
                    'INFO',
                    f"Call forward transfer initiated to company: {check_individual_company_avaliabe}",
                    conversation_state
                )
                
        elif disposition == 'Truck Rental':
            # Log start of truck rental process
            await log_call_action(
                conversation_state['lead_id'],
                'TRUCK_RENTAL_START',
                'INFO',
                "Starting truck rental disposition process",
                conversation_state
            )
            
            check_company_avaliabe = await company_avaliability(conversation_state['lead_id'], 2)
            
            # Log truck rental company availability
            await log_call_action(
                conversation_state['lead_id'],
                'TRUCK_COMPANY_AVAILABILITY_CHECK',
                'INFO' if check_company_avaliabe else 'WARNING',
                f"Truck rental company availability result: {check_company_avaliabe}",
                conversation_state
            )
            
            if check_company_avaliabe == None:
                ai_greeting_instruction = "This buyer not available at this moment and no other buyer available"
                ai_instruct = f"This buyer not available at this moment and no other buyer available"
                
                # Log no truck rental companies available
                await log_call_action(
                    conversation_state['lead_id'],
                    'NO_TRUCK_RENTAL_COMPANIES',
                    'WARNING',
                    "No truck rental companies available",
                    conversation_state
                )
                
                await ai_instract_guid(
                    openai_ws, ai_greeting_instruction, item_id, call_id, 
                    conversation_state, 'No Buyer', ai_instruct, follow_up_time, 1
                )
            else:
                ai_greeting_instruction = "Yes we have moving company avaliable"
                
                # Log truck rental company available
                await log_call_action(
                    conversation_state['lead_id'],
                    'TRUCK_COMPANY_AVAILABLE',
                    'INFO',
                    f"Truck rental company available: {check_company_avaliabe}",
                    conversation_state
                )
                
                transfer_result = await transfer_call(
                    conversation_state['lead_id'], 2, 
                    conversation_state['site'], conversation_state['server']
                )
                
                # Log truck rental transfer initiated
                await log_call_action(
                    conversation_state['lead_id'],
                    'TRUCK_TRANSFER_INITIATED',
                    'INFO',
                    "Truck rental transfer initiated",
                    conversation_state
                )

        elif disposition == 'Agent Transfer':
            # Log start of agent transfer process
            await log_call_action(
                conversation_state['lead_id'],
                'AGENT_TRANSFER_START',
                'INFO',
                "Starting agent transfer disposition",
                conversation_state
            )
            
            est = pytz.timezone("America/New_York")
            est_time = datetime.now(est)
            current_hour = est_time.hour
            
            # Log business hours check
            await log_call_action(
                conversation_state['lead_id'],
                'BUSINESS_HOURS_CHECK',
                'INFO',
                f"Current hour in EST: {current_hour}",
                conversation_state
            )
            
            if 8 <= current_hour < 18:
                print("YES: Time is between 8 AM and 6 PM")
                
                # Log within business hours
                await log_call_action(
                    conversation_state['lead_id'],
                    'WITHIN_BUSINESS_HOURS',
                    'INFO',
                    "Within business hours, proceeding with agent transfer",
                    conversation_state
                )
                
                await dispostion_status_update(
                    conversation_state['lead_id'], 
                    disposition, 
                    follow_up_time
                )
            else:
                print("NO: Time is outside 8 AM - 6 PM")
                next_run_time = (est_time + timedelta(days=1)).replace(
                    hour=8, minute=0, second=0, microsecond=0
                )
                ai_greeting_instruction = "I've saved the Follow Up disposition. because it's outside of business hours"
                
                # Log outside business hours
                await log_call_action(
                    conversation_state['lead_id'],
                    'OUTSIDE_BUSINESS_HOURS',
                    'INFO',
                    f"Outside business hours. Current: {current_hour}:00 EST. Scheduled for: {next_run_time}",
                    conversation_state
                )
                
                saved_output = {
                    "status": "saved",
                    "disposition": 'Follow Up',
                    "customer_response": ai_greeting_instruction,
                    "followup_time": '',
                    "notes": ai_greeting_instruction,
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

                # Log delayed disposition scheduling
                await log_call_action(
                    conversation_state['lead_id'],
                    'DELAYED_DISPOSITION_SCHEDULED',
                    'INFO',
                    f"Disposition scheduled for: {next_run_time}",
                    conversation_state
                )
                
                # schedule delayed disposition update
                asyncio.create_task(
                    delayed_disposition_update(
                        conversation_state['lead_id'], 
                        "Follow Up", 
                        next_run_time
                    )
                )
                
                # Log end of agent transfer process
                await log_call_action(
                    conversation_state['lead_id'],
                    'AGENT_TRANSFER_COMPLETE',
                    'INFO',
                    "Agent transfer process completed (delayed due to business hours)",
                    conversation_state
                )
                
                return

        else:
            # Log other disposition types
            await log_call_action(
                conversation_state['lead_id'],
                'OTHER_DISPOSITION_UPDATE',
                'INFO',
                f"Updating disposition to: {disposition} with follow-up: {follow_up_time}",
                conversation_state
            )
            
            await dispostion_status_update(
                conversation_state['lead_id'], 
                disposition, 
                follow_up_time
            )
            
            ai_greeting_instruction = "I've saved the disposition. Is there anything else you'd like to do?"

    # ----- Handle transfer_result if present -----
    parsed_transfer = None
    transfer_failed = False
    transfer_error_message = None

    if transfer_result:
        parsed_transfer = ensure_dict(transfer_result)
        
        # Log raw transfer result
        await log_call_action(
            conversation_state['lead_id'],
            'TRANSFER_RESULT_RAW',
            'INFO',
            f"Raw transfer result: {parsed_transfer}",
            conversation_state
        )
        
        print('tresult', parsed_transfer)
        try:
            status = parsed_transfer.get("status")
            transfer_error_message = parsed_transfer.get("error") or parsed_transfer.get("data") or "Transfer failed."
            print('status', status)
            if status == "FAILURE":
                transfer_failed = True
                
                # Log transfer failure
                await log_call_action(
                    conversation_state['lead_id'],
                    'TRANSFER_FAILURE',
                    'ERROR',
                    f"Transfer failed with error: {transfer_error_message}",
                    conversation_state
                )
                
                await dispostion_status_update(
                    conversation_state['lead_id'], 
                    "No Buyer",
                    follow_up_time
                )
                
                ai_greeting_instruction = transfer_error_message
            else:
                # Log transfer success
                await log_call_action(
                    conversation_state['lead_id'],
                    'TRANSFER_SUCCESS',
                    'SUCCESS',
                    f"Transfer successful. Result: {parsed_transfer}",
                    conversation_state
                )
        except Exception as e:
            print("Transfer result parsing error:", e)
            
            # Log parsing error
            await log_call_action(
                conversation_state['lead_id'],
                'TRANSFER_PARSE_ERROR',
                'ERROR',
                f"Error parsing transfer result: {e}",
                conversation_state
            )
            
            transfer_failed = True
            transfer_error_message = "Transfer failed (parse error)."
            ai_greeting_instruction = transfer_error_message

    # ----- Build saved_output based on transfer outcome -----
    if transfer_failed:
        # include the API error message and the parsed transfer result in the function output
        saved_output = {
            "status": "saved",
            "disposition": args.get("disposition") or 'Follow Up',
            "customer_response": transfer_error_message,
            "followup_time": args.get("followup_time"),
            "notes": transfer_error_message,
            "transfer_result": parsed_transfer,
            "api_error": transfer_error_message,
        }
        
        # Log failed output structure
        await log_call_action(
            conversation_state['lead_id'],
            'FAILED_OUTPUT_CREATED',
            'INFO',
            f"Created failed output structure for OpenAI",
            conversation_state
        )
    else:
        # normal saved output (no transfer failure)
        saved_output = {
            "status": "saved",
            "disposition": 'Follow Up',
            "customer_response": args.get("customer_response"),
            "followup_time": args.get("followup_time"),
            "notes": args.get("notes"),
        }
        
        # Log successful output structure
        await log_call_action(
            conversation_state['lead_id'],
            'SUCCESS_OUTPUT_CREATED',
            'INFO',
            f"Created successful output structure for OpenAI",
            conversation_state
        )

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
    
    # Log OpenAI response sent
    await log_call_action(
        conversation_state['lead_id'],
        'OPENAI_RESPONSE_SENT',
        'INFO',
        f"Sent response to OpenAI with item_id: {item_id}",
        conversation_state
    )

    # 2️⃣ Tell the model to speak confirmation / error
    await openai_ws.send(json.dumps({
        "type": "response.create",
        "response": {
            "modalities": ["audio", "text"],
            "instructions": ai_greeting_instruction
        }
    }))
    
    # Log audio response instruction
    await log_call_action(
        conversation_state['lead_id'],
        'AUDIO_RESPONSE_INSTRUCTED',
        'INFO',
        f"Audio response instruction: {ai_greeting_instruction[:100]}...",
        conversation_state
    )
    
    # Log completion of disposition assignment
    await log_call_action(
        conversation_state['lead_id'],
        'DISPOSITION_ASSIGNMENT_COMPLETE',
        'INFO',
        f"Completed disposition assignment. Final instruction: {ai_greeting_instruction[:100]}...",
        conversation_state
    )


async def handle_ma_lead_set_call_disposition(openai_ws, args, item_id, call_id, conversation_state):
    print("\n=== Saving Disposition ===")
    print(args)
    ai_greeting_instruction = ''
    transfer_result = None
    follow_up_time = args.get("followup_time")

    if args.get("disposition") is not None:
        if args.get("disposition") == 'Live Transfer':
                ai_greeting_instruction = "Yes we have moving company avaliable"
                transfer_result = await transfer_ma_lead_call(conversation_state['lead_id'], 1, conversation_state['t_call_id'], conversation_state['lead_phone'], conversation_state['site'], conversation_state['server'])
        else:
            await set_ma_lead_dispostion_status_update(conversation_state['lead_id'], args.get("disposition"), conversation_state['t_call_id'], conversation_state['lead_phone'], follow_up_time)
            ai_greeting_instruction = "I've saved the disposition. Is there anything else you'd like to do?"

    # ----- Handle transfer_result if present -----
    parsed_transfer = None
    transfer_failed = False
    transfer_error_message = None

    if transfer_result:
        parsed_transfer = ensure_dict(transfer_result)
        print('tresult', parsed_transfer)
        try:
            status = parsed_transfer.get("status")
            transfer_error_message = parsed_transfer.get("error") or parsed_transfer.get("data") or "Transfer failed."
            print('status', status)
            if status == "FAILURE":
                transfer_failed = True
                await set_ma_lead_dispostion_status_update(conversation_state['lead_id'], "No Buyer", conversation_state['t_call_id'], conversation_state['lead_phone'], follow_up_time)
                ai_greeting_instruction = transfer_error_message  # make model speak the error
        except Exception as e:
            print("Transfer result parsing error:", e)
            # keep going; don't crash — but set a generic message
            transfer_failed = True
            transfer_error_message = "Transfer failed (parse error)."
            ai_greeting_instruction = transfer_error_message

    # ----- Build saved_output based on transfer outcome -----
    if transfer_failed:
        # include the API error message and the parsed transfer result in the function output
        saved_output = {
            "status": "saved",
            "disposition": args.get("disposition") or 'Follow Up',
            "customer_response": transfer_error_message,
            "followup_time": args.get("followup_time"),
            "notes": transfer_error_message,
            "transfer_result": parsed_transfer,   # full parsed transfer result for debugging/record
            "api_error": transfer_error_message,
        }
    else:
        # normal saved output (no transfer failure)
        saved_output = {
            "status": "saved",
            "disposition": 'Follow Up',
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

    # 2️⃣ Tell the model to speak confirmation / error (audio + text)
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

            if conversation_state.get('site','').lower() == 'ma':
                if re.search(r'\bstudio(\s*(apartment|room))?\b', move_size_lower):
                    update_data['move_size'] = 1
                    api_update_data['move_size'] = args['move_size']
                elif re.search(r'\b(1\s*bed(room)?s?|one\s*bed(room)?s?|1\s*br|1\s*bhk)\b', move_size_lower):
                    update_data['move_size'] = 2
                    api_update_data['move_size'] = args['move_size']
                elif re.search(r'\b(2\s*bed(room)?s?|two\s*bed(room)?s?|2\s*br|2\s*bhk)\b', move_size_lower):
                    update_data['move_size'] = 3
                    api_update_data['move_size'] = args['move_size']
                elif re.search(r'\b(3\s*bed(room)?s?|three\s*bed(room)?s?|3\s*br|3\s*bhk)\b', move_size_lower):
                    update_data['move_size'] = 4
                    api_update_data['move_size'] = args['move_size']
                elif re.search(r'\b(4\s*bed(room)?s?|four\s*bed(room)?s?|4\s*br|4\s*bhk)\b', move_size_lower):
                    update_data['move_size'] = 5
                    api_update_data['move_size'] = args['move_size']
                elif re.search(r'\b(5\s*\+|5\s*bed(room)?s?|five\s*bed(room)?s?|5\s*br|5\s*bhk)\b', move_size_lower):
                    update_data['move_size'] = 6
                    api_update_data['move_size'] = args['move_size']
                else:
                    update_data['move_size'] = args['move_size']
                    api_update_data['move_size'] = args['move_size']
            else:    
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


# ===============================================================
# HANDLE FUNCTION: Ma lead function start
# ===============================================================

async def send_inventory_link(openai_ws, args, item_id, call_id, conversation_state):
    print("\n=== Sending Inventory Link ===")
    print(args)
    # Implement the logic to send inventory link here
    # For now, just send a confirmation back to OpenAI

    payload = {
        "lead_numbers_id": conversation_state.get('lead_numbers_id'),
        "message": args.get('inventory_link', ''),
        "type": "text"
    }
    
    api_success = await sms_send_or_not_fun(
        conversation_state.get('site'), 
        conversation_state.get('server'), 
        payload
    )
    print(f"[TRANSFER] API call result: {api_success}")
    await openai_ws.send(json.dumps({
        "type": "conversation.item.create",
        "item": {
            "id": item_id,
            "type": "function_call_output",
            "call_id": call_id,
            "output": json.dumps({"status": "Inventory link sent"})
        }
    }))
    await openai_ws.send(json.dumps({
        "type": "response.create",
        "response": {
            "modalities": ["audio", "text"],
            "instructions": "The inventory link has been sent successfully."
        }
    }))

async def send_payment_link(openai_ws, args, item_id, call_id, conversation_state):
    print("\n=== Sending Payment Link ===")
    print(args)
    # Implement the logic to send payment link here
    # For now, just send a confirmation back to OpenAI

    payload = {
        "lead_numbers_id": conversation_state.get('lead_numbers_id'),
        "message": args.get('payment_link', ''),
        "type": "text"
    }
    
    api_success = await sms_send_or_not_fun(
        conversation_state.get('site'), 
        conversation_state.get('server'), 
        payload
    )
    print(f"[TRANSFER] API call result: {api_success}")
    await openai_ws.send(json.dumps({
        "type": "conversation.item.create",
        "item": {
            "id": item_id,
            "type": "function_call_output",
            "call_id": call_id,
            "output": json.dumps({"status": "Payment link sent"})
        }
    }))
    await openai_ws.send(json.dumps({
        "type": "response.create",
        "response": {
            "modalities": ["audio", "text"],
            "instructions": "The payment link has been sent successfully."
        }
    }))

async def send_invoice_link(openai_ws, args, item_id, call_id, conversation_state):
    print("\n=== Sending Invoice Link ===")
    print(args)
    
    payload = {
        "lead_numbers_id": conversation_state.get('lead_numbers_id'),
        "message": args.get('invoice_link', ''),
        "type": "text"
    }
    
    api_success = await sms_send_or_not_fun(
        conversation_state.get('site'), 
        conversation_state.get('server'), 
        payload
    )
    print(f"[TRANSFER] API call result: {api_success}")

    await openai_ws.send(json.dumps({
        "type": "conversation.item.create",
        "item": {
            "id": item_id,
            "type": "function_call_output",
            "call_id": call_id,
            "output": json.dumps({"status": "Invoice link sent"})
        }
    }))
    await openai_ws.send(json.dumps({
        "type": "response.create",
        "response": {
            "modalities": ["audio", "text"],
            "instructions": "The invoice link has been sent successfully."
        }
    }))


async def add_lead_note(openai_ws, args, item_id, call_id, conversation_state):
    print("\n=== Adding Lead Note ===")
    print(args)
    
    payload = {
        "lead_numbers_id": conversation_state.get('lead_numbers_id'),
        "message": args.get('note', ''),
        "type": "note"
    }
    
    api_success = await sms_send_or_not_fun(
        conversation_state.get('site'), 
        conversation_state.get('server'), 
        payload
    )
    print(f"[TRANSFER] API call result: {api_success}")

    # Send response back to OpenAI
    await openai_ws.send(json.dumps({
        "type": "conversation.item.create",
        "item": {
            "id": item_id,
            "type": "function_call_output",
            "call_id": call_id,
            "output": json.dumps({"status": "Note added successfully"})
        }
    }))
    
    await openai_ws.send(json.dumps({
        "type": "response.create",
        "response": {
            "modalities": ["audio", "text"],
            "instructions": "The note has been added to the lead successfully."
        }
    }))
    
    return True


async def sms_send_or_not_fun(site, server, payload):
    """
    Sends SMS via external API if site is "MA".
    - For Prod server: Uses the production URL
    - For other servers: Uses the development URL
    Returns True on successful API call, False otherwise.
    If site is not "MA", returns True (no API call needed).
    """
    print(f"[TRANSFER] Checking SMS send conditions: site={site}, server={server}")
    
    # If site is not MA, don't make API call
    if site != "MA":
        print(f"[TRANSFER] Site is not MA ({site}), skipping API call")
        return True
    
    # Determine which URL to use based on server
    if server == "Prod":
        url = "https://zapprod:zap2024@zap.snapit.software/api/calltransfertest"
    else:
        url = "https://developer.leaddial.co/developer/api/tenant/lead/send-customer-sms"
    
    print(f"[TRANSFER] Using URL: {url}")
    
    # Make the API call
    async with aiohttp.ClientSession() as session:
        await asyncio.sleep(2)
        try:
            async with session.post(
                url,
                headers={'Content-Type': 'application/json'},
                data=json.dumps(payload),
                timeout=aiohttp.ClientTimeout(total=10)  # Added timeout
            ) as resp:
                if resp.status == 200:
                    response_text = await resp.json()
                    print(f"[TRANSFER] API call successful: {response_text}")
                    return True
                else:
                    response_text = await resp.json()
                    print(f"[TRANSFER] API call failed with status {resp.status}: {response_text}")
                    return False
        except Exception as e:
            print(f"[TRANSFER] API call error: {e}")
            return False

# ===============================================================
# HANDLE FUNCTION: Ma lead function end
# ===============================================================

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
        elif site == "MA":
            if server == "Prod":
                url = "https://malead:ma2024@ma.snapit.software/api/updateailead"
            else:
                url = "https://developer.leaddial.co/developer/api/tenant/lead/update-customer-info"
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

                # collect contact entries (keys from your API are "live_transfer" and "truck_rental_transfer" and "call_forward_transfer")
                contacts_to_insert = []
                call_types_seen = set()

                # NOTE: iterate explicit (api_node_key, call_type_value) pairs
                pairs = (
                    ("live_transfer", "live_transfer"),
                    ("truck_rental_transfer", "truck_rental_transfer"),
                    ("call_forward_transfer", "call_forward_transfer"),
                )

                for key, call_type in pairs:
                    node = data.get(key)
                    if node and isinstance(node, dict):
                        campaign_id = node.get("campaign_id") or node.get("campaign_id") or None
                        campaign_payout = node.get("mover_payout") or node.get("mover_payout") or None
                        campaign_score = node.get("mover_score") or node.get("mover_score") or None
                        name = node.get("mover_name") or node.get("name") or None
                        phone = node.get("mover_phone") or node.get("phone") or None
                        if not (name or phone):
                            print(f"[TRANSFER] Skipping {key} — no name/phone present")
                            continue
                        call_types_seen.add(call_type)
                        print(f"[TRANSFER] call_type {call_type} for phone {phone}")
                        normalized_phone = normalize_phone(phone) if phone else None
                        if call_type == 'live_transfer' and normalized_phone:
                            try:
                                conn2 = get_db_connection()
                                if not conn2:
                                    raise Exception("DB connection failed")
                                cur2 = conn2.cursor()
                                lead_id_val = int(ret_lead_id) if (ret_lead_id is not None and str(ret_lead_id).isdigit()) else (ret_lead_id or 0)
                                cur2.execute("UPDATE leads SET mover = %s, mover_phone = %s, campaign_id = %s, campaign_payout = %s, campaign_score = %s WHERE lead_id = %s",
                                             (name, normalized_phone, campaign_id, campaign_payout, campaign_score,lead_id_val))
                                conn2.commit()
                                cur2.close()
                                conn2.close()
                                print(f"[TRANSFER] Updated leads({lead_id_val}) mover and mover_phone")
                            except Exception as e:
                                print(f"[TRANSFER] Failed to update leads table: {e}")
                        elif call_type == 'truck_rental_transfer' and normalized_phone:
                            try:
                                conn2 = get_db_connection()
                                if not conn2:
                                    raise Exception("DB connection failed")
                                cur2 = conn2.cursor()
                                lead_id_val = int(ret_lead_id) if (ret_lead_id is not None and str(ret_lead_id).isdigit()) else (ret_lead_id or 0)
                                cur2.execute("UPDATE leads SET campaign_id_truck = %s, campaign_payout_truck = %s, campaign_score_truck = %s WHERE lead_id = %s",
                                             (campaign_id, campaign_payout, campaign_score,lead_id_val))
                                conn2.commit()
                                cur2.close()
                                conn2.close()
                                print(f"[TRANSFER] Updated leads({lead_id_val}) truck_mover and mover_phone")
                            except Exception as e:
                                print(f"[TRANSFER] Failed to update leads table: {e}")
                        
                        contacts_to_insert.append({
                            "lead_id": int(ret_lead_id) if (ret_lead_id is not None and str(ret_lead_id).isdigit()) else (ret_lead_id or 0),
                            "calluuid": call_u_id or api_update_data.get('calluuid') or '',
                            "call_type": call_type,
                            "name": name,
                            "phone": normalize_phone(phone),
                            "campaign_id": campaign_id,
                            "campaign_payout": campaign_payout,
                            "campaign_score": campaign_score,
                        })

                if not contacts_to_insert:
                    print("[TRANSFER] No live_transfer/truck_rental data to save.")
                    # try:
                    #     conn = get_db_connection()
                    #     if not conn:
                    #         raise Exception("Failed to get DB connection")

                    #     cursor = conn.cursor(buffered=True)

                    #     delete_sql = """
                    #         DELETE FROM lead_call_contact_details
                    #         WHERE lead_id = %s
                    #     """

                    #     print(f"[TRANSFER] Deleting existing contacts for lead_id={lead_id}")
                    #     cursor.execute(delete_sql, (lead_id,))

                    #     conn.commit()
                    #     cursor.close()
                    #     print("[TRANSFER] Existing records deleted successfully.") 
                    # except Exception as e:
                    #     print(f"[TRANSFER] Error while deleting records: {e}")
                    #     return False
                    return True

                # Insert contacts into DB, deleting existing ones for this lead & call_types first (if lead_id provided)
                conn = get_db_connection()
                if not conn:
                    print("[TRANSFER] Failed to get DB connection to insert contacts")
                    return True  # external API succeeded; return True but log DB issue

                try:
                    cur = conn.cursor()
                    try:
                        # Ensure lead_id is valid before running delete
                        if ret_lead_id and str(ret_lead_id).isdigit():
                            lead_id_int = int(ret_lead_id)

                            delete_sql = """
                                DELETE FROM lead_call_contact_details
                                WHERE lead_id = %s
                            """

                            print(f"[TRANSFER] Deleting existing contacts for lead_id={lead_id_int}")
                            cur.execute(delete_sql, (lead_id_int,))
                            print(f"[TRANSFER] Deleted {cur.rowcount} existing contact(s)")
                        else:
                            print("[TRANSFER] Invalid or missing lead_id; skipping delete step.")

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


async def transfer_ma_lead_call(lead_id,transfer_type,t_lead_id,lead_phone,site,server):
        try:
            print(f"[TRANSFER] Starting call transfer for lead_id: {lead_id}")
            
            # Fetch lead data from database
            
            # Prepare the payload
            payload = {
            'lead_id': lead_id,
            'lead_ob_call_id': t_lead_id,
            'disposition': "transfer",
            'lead_phone': lead_phone,
            'follow_up_date': "",
            'quote_sent_date': "",  # defaults to 0 if None or missing
            'lead_IB_call_id': "",
            }
            
            print(f"[TRANSFER] Payload: {payload}")
            # Determine URL based on phone number

            url = "https://developer.leaddial.co/developer/cron/tenant/agent-call-center/save-call-me-now-ai"
            
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


async def company_avaliability(lead_id, transfer_type):
    try:
        print(f"[TRANSFER] Starting call transfer for lead_id: {lead_id}, transfer_type: {transfer_type}")
        conn = get_db_connection()
        if not conn:
            raise Exception("Failed to get database connection")

        cursor = conn.cursor(dictionary=True, buffered=True)

        company_details = None

        if transfer_type == 1:
            # Live transfer
            cursor.execute(
                """
                SELECT * FROM lead_call_contact_details
                WHERE lead_id = %s AND
                call_type IN ('live_transfer', 'live_trasnfer')
                """,
                (lead_id,)
            )
        elif transfer_type == 2:
            # Truck rental transfer
            cursor.execute(
                """
                SELECT * FROM lead_call_contact_details
                WHERE lead_id = %s AND call_type = 'truck_rental_transfer'
                """,
                (lead_id,)
            )

        company_details = cursor.fetchone()
        print("[DEBUG] Fetched company_details:", company_details)

        cursor.close()
        conn.close()

        if company_details and company_details.get("phone"):
            return company_details["phone"]
        else:
            return None

    except Exception as e:
        print(f"[TRANSFER ERROR] {e}")
        return None


async def check_individual_company_avaliability(lead_id,company_name):
    try:
        print(f"[TRANSFER] Starting call transfer for lead_id: {lead_id}")
        conn = get_db_connection()
        if not conn:
            raise Exception("Failed to get database connection")

        cursor = conn.cursor(dictionary=True, buffered=True)

        company_details = None

        cursor.execute(
            """
            SELECT * FROM lead_call_contact_details
            WHERE lead_id = %s AND
            call_type = 'call_forward_trasnfer' AND name = %s
            """,
            (lead_id,company_name)
        )

        company_details = cursor.fetchone()
        print("[DEBUG] Fetched company_details:", company_details)

        cursor.close()
        conn.close()

        if company_details and company_details.get("phone"):
            return company_details
        else:
            return None

    except Exception as e:
        print(f"[TRANSFER ERROR] {e}")
        return None


async def call_forward_transfer_call(lead_id, company_details, site, server):
    try:
        print(f"[TRANSFER] Starting call transfer for lead_id: {lead_id}")
        print(f"[TRANSFER] company_details: {company_details}")
        print(f"[TRANSFER] site: {site}")
        print(f"[TRANSFER] server: {server}")
        
        # Database connection and query
        conn = get_db_connection()
        if conn:
            cursor = conn.cursor(dictionary=True, buffered=True)
            cursor.execute("SELECT * FROM leads WHERE lead_id = %s", (lead_id,))
            lead_data = cursor.fetchone()
            cursor.close()
            conn.close()
        else:
            raise Exception("Failed to get database connection")
            
        if not lead_data:
            raise Exception(f"Lead not found for lead_id: {lead_id}")

        # Prepare payload
        payload = {
            'id': lead_data.get('t_call_id'),
            'action': 1,
            'usaBusinessCheck': 2,
            'type': 1,
            'review_call': lead_data.get('review_call', 0),  # defaults to 0 if None or missing
            'accept_call': 0,
            'rep_id': lead_data.get('t_rep_id'),
            'logic_check': 1,
            'lead_id': lead_data.get('t_lead_id'),
            'categoryId': 1,
            'buffer_id_arr': '',
            'campaignId': company_details.get('campaign_id'),
            'campaignScore': company_details.get('campaign_score'),
            'campaignNumber': company_details.get('phone'),
            'campaignPayout': company_details.get('campaign_payout')
        }
        
        print(f"[TRANSFER] Payload: {json.dumps(payload, indent=2)}")

        # Determine URL based on site and server
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
            # ✅ Add delay before making API call
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
        print(f"[TRANSFER ERROR] {e}")
        return None

async def call_forward_company_avaliability(lead_id):
    try:
        print(f"[TRANSFER] Starting call transfer for lead_id: {lead_id}")
        conn = get_db_connection()
        if not conn:
            raise Exception("Failed to get database connection")

        cursor = conn.cursor(dictionary=True, buffered=True)

        cursor.execute(
            """
            SELECT 
                GROUP_CONCAT(DISTINCT name SEPARATOR ', ') as company_names
            FROM lead_call_contact_details
            WHERE lead_id = %s 
                AND call_type = 'call_forward_trasnfer' 
                AND name IS NOT NULL 
                AND name != ''
            GROUP BY lead_id
            """,
            (lead_id,)
        )

        result = cursor.fetchone()
        print("[DEBUG] Fetched company names:", result)

        cursor.close()
        conn.close()

        if result and result.get("company_names"):
            return result["company_names"]
        else:
            return None

    except Exception as e:
        print(f"[TRANSFER ERROR] {e}")
        return None        

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
            print('followup time:',follow_up_time)
            dt = datetime.fromisoformat(follow_up_time)
            dt_utc = pytz.utc.localize(dt)
            est_time = dt_utc.astimezone(pytz.timezone("America/New_York"))
            print('converted followup time:',follow_up_time)
            #est = pytz.timezone("America/New_York")
            #est_time = datetime.now(est)
            #current_hour = est_time.hour
            params["followupdatetime"] = est_time
            print(f"[DISPOSITION] Lead {lead_id} disposition updated to {disposition}")
        # Build the URL with proper encoding
        query_string = urlencode(params, quote_via=quote)
        redirect_url = f"http://54.176.128.91/disposition_route?{query_string}"
        print(f"[DISPOSITION] Redirect URL: {redirect_url}")
        # Send request
        response = requests.post(redirect_url)
        api_response_text = response.text if response.text else str(response)

        print(f"[DISPOSITION] Lead {lead_id} disposition updated to {disposition}")

        # -------------------------------
        # SAVE API CALL LOG IN DATABASE
        # -------------------------------
        conn = get_db_connection()
        if conn:
            try:
                cursor = conn.cursor()
                insert_query = """
                    INSERT INTO dispotion_api_call_logs (lead_id, api_url, api_response, dispotion)
                    VALUES (%s, %s, %s, %s)
                """
                cursor.execute(insert_query, (lead_id, redirect_url, api_response_text, disposition_val))
                conn.commit()
                print(f"[LOG] API call stored for lead {lead_id}")
            except Exception as db_error:
                print(f"[DB ERROR] Failed to insert log: {db_error}")
            finally:
                cursor.close()
                conn.close()

    except Exception as e:
        print(f"[DISPOSITION] Error updating lead disposition: {e}")


async def set_ma_lead_dispostion_status_update(lead_id, disposition_val, t_call_id, lead_phone, follow_up_time):
    try:

        if disposition_val == 'voice message':
            disposition = 1
        elif disposition_val == 'DNC':
            disposition = 2
        elif disposition_val == 'wrong phone':
            disposition = 3
        elif disposition_val == 'not interested':
            disposition = 4
        elif disposition_val == 'follow up':
            disposition = 5
        elif disposition_val == 'booked':
            disposition = 6
        elif disposition_val == 'booked with others':
            disposition = 7
        elif disposition_val == 'disconnected':
            disposition = 8
        elif disposition_val == 'no disposition':
            disposition = 9
        elif disposition_val == 'No Coverage':
            disposition = 10
        elif disposition_val == 'hang up':
            disposition = 11
        elif disposition_val == 'Quote Sent':
            disposition = 12
        elif disposition_val == 'transfer':
            disposition = 13
        else:
            disposition = 14

        params = {
                "lead_id": lead_id,
                "lead_ob_call_id": t_call_id,
                "disposition": disposition_val,
                "lead_phone": lead_phone,
                "quote_sent_date":"",
                "lead_IB_call_id":""
            }
        print('params',params)
        if disposition == 5:
            print('followup time:',follow_up_time)
            dt = datetime.fromisoformat(follow_up_time)
            dt_utc = pytz.utc.localize(dt)
            est_time = dt_utc.astimezone(pytz.timezone("America/New_York"))
            print('converted followup time:',follow_up_time)
            #est = pytz.timezone("America/New_York")
            #est_time = datetime.now(est)
            #current_hour = est_time.hour
            params["follow_up_date"] = est_time
            print(f"[DISPOSITION] Lead {lead_id} disposition updated to {disposition}")
        
        # Build the new API URL and payload for LeadDial
        api_url = "https://developer.leaddial.co/developer/cron/tenant/agent-call-center/set-disposition-ai"
        
        
        # Send request to the new API
        headers = {'Content-Type': 'application/json'}
        response = requests.post(api_url, json=params, headers=headers)
        api_response_text = response.text if response.text else str(response)
        print(f"[DISPOSITION] Response: {response}")
        print(f"[DISPOSITION] API Response: {api_response_text}")
        print(f"[DISPOSITION] Lead {lead_id} disposition updated to {disposition}")

        # -------------------------------
        # SAVE API CALL LOG IN DATABASE
        # -------------------------------
        conn = get_db_connection()
        if conn:
            try:
                cursor = conn.cursor()
                insert_query = """
                    INSERT INTO dispotion_api_call_logs (lead_id, api_url, api_response, dispotion)
                    VALUES (%s, %s, %s, %s)
                """
                cursor.execute(insert_query, (lead_id, api_url, api_response_text, disposition_val))
                conn.commit()
                print(f"[LOG] API call stored for lead {lead_id}")
            except Exception as db_error:
                print(f"[DB ERROR] Failed to insert log: {db_error}")
            finally:
                cursor.close()
                conn.close()

    except Exception as e:
        print(f"[DISPOSITION] Error updating lead disposition: {e}")


async def ai_instract_guid(openai_ws, ai_greeting_instruction, item_id, call_id, conversation_state,instructions,dispotion,follow_up_time,stype):
    saved_output = {
        "status": "saved",
        "disposition": dispotion,
        "customer_response": ai_greeting_instruction,
        "followup_time": "",
        "notes": ai_greeting_instruction,
    }

    # 1️⃣ Send function output back to OpenAI (Follow Up saved because out of hours)
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
             "instructions": instructions
         }
     }))

    if stype == 1:
        # schedule delayed disposition update (runs async in background)
        asyncio.create_task(
            delayed_disposition_update(conversation_state['lead_id'], dispotion, follow_up_time)
        )
# ===============================================================
# SEND SESSION UPDATE (HOSTED PROMPT ONLY)
# ===============================================================
async def send_Session_update(openai_ws,prompt_to_use,brand_id,lead_data_result):

    if brand_id == '5':
        print("Ma-only")
        prompt_obj = {
            "id": "pmpt_6949c64757788194b81db1cb113fda3d0723a6832edde4a7"
        }
    elif lead_data_result == '1':
        if brand_id == '2':
            print("vm-outbound")
            prompt_obj = {
                "id": "pmpt_69262d5672f4819399859365246218520c851a4afbab2899"
            }
        else:
            print("outbound")
            prompt_obj = {
                "id": "pmpt_69175111ddb88194b4a88fc70e6573780dfc117225380ded"
            }
    else:
        print("inbound")
        prompt_obj = {
            "id": "pmpt_691652392c1c8193a09ec47025d82ac305f13270ca49da07"
        }
    print('prompt_check',prompt_to_use)
    # Force English responses
    session_update = {
        "type": "session.update",
        "session": {
            "prompt": prompt_obj,
            "instructions": prompt_to_use,
            "input_audio_format": "g711_ulaw",
            "output_audio_format": "g711_ulaw",
            "input_audio_transcription": {"model": "whisper-1", "language": "en"},  # Enable transcription
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