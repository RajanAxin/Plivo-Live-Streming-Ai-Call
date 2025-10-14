
import plivo
import difflib
from quart import Quart, websocket, Response, request, session
from fastapi import Query
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
import aiohttp
import re
import html
import time
import openai
import csv

load_dotenv(dotenv_path='.env', override=True)

OPENAI_API_KEY = os.getenv('OPENAI_API_KEY')
PLIVO_AUTH_ID = os.getenv('PLIVO_AUTH_ID')
PLIVO_AUTH_TOKEN = os.getenv('PLIVO_AUTH_TOKEN')
openai.api_key = OPENAI_API_KEY
if not OPENAI_API_KEY:
    raise ValueError("OPENAI_API_KEY environment variable is not set. Please add it to your .env file")

PORT = 5000

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
                    "instruction": row["Micro-Script or Instruction (EN)"].strip(),
                    "next_action": row["NextAction"].strip()
                })
            print(f"Loaded {len(rules)} conversation rules from CSV")
            return rules
    except Exception as e:
        print(f"Error loading conversation flow CSV: {e}")
        return None

def build_system_message(rules=None):
    """Build system message with dynamic rules integration"""
    base_prompt = """
You are a friendly, professional, emotionally aware virtual moving assistant.
Your #1 goal: connect the caller live to a moving representative for the best quote as soon as they agree.

Data you can rely on

Lead data (pre-filled):
Name: [lead_name]
Phone: [lead_phone]
Email: [lead_email]
Origin: [lead_from_city], [lead_from_state] [lead_from_zipcode]
Destination: [lead_to_city], [lead_to_state] [lead_to_zipcode]
Move Date: [lead_move_date]
Move Size: [lead_move_size]

Tag Spreadsheet (provided at session start as rows with these columns):
TagID | Phase | Tag | Subtag | Purpose | Triggers (examples, ;-separated) | RequiredFacts (needed before next) | Micro-Script or Instruction (EN) | NextAction
You must select exactly one row per turn and use its Micro-Script (1–2 sentences max), then execute NextAction.

Actions/Tools available:
offer_live_transfer(partner_hint?), send_text_link(kind="quote"), transfer_to_dialup(), schedule_callback(time_window), mark_dnc(), crm_update(fields), check_availability(from_zip,to_zip,move_date,size_or_items).
Conversation style & behavior

Start friendly, by name: “Hi [lead_name], how are you today?” Then 1 short line: “I’m calling about your move from [from_city] to [to_city] on [move_date] to get you the best quote.”

One question at a time. Keep every reply 1–2 short sentences, then stop.

Never repeat a question. Use lead data you already have. Only ask for details that are missing or unclear.

Interrupt-friendly: if the caller starts speaking, stop immediately.

Primary outcome: as soon as interest is warm, ask: “Want me to connect you live to a mover for the best quote?”

Pricing: do not give exact prices. If asked “how much/price,” connect them to a mover who will provide a quick ballpark based on date & items.

Truck-rental persuasion: If they mention U-Haul/Penske/renting a truck:

Line 1: “DIY adds fuel, tolls, equipment, and your time. Full-service is one clear price and can be cheaper overall.”

Line 2: “Pros handle the lifting and driving so you keep your day. Delivery is scheduled to fit your timing.”
Then ask to connect.

Access/big items/storage: Only if it helps rapport or improves the quote; otherwise skip. Ask about storage only if the caller hints at it.

Routing: Anything not about getting a quote (COI, reschedule, cancel, claims, invoices) → Support (DialUp).

Spanish: If Spanish is detected or requested, route to Support (DialUp) (do not handle Spanish here).

Do-Not-Call: If requested, confirm once and end: “Understood—I’ve added you to Do-Not-Call and won’t contact you again. Wishing you a smooth move.”
Slot logic (the “4 basics”)

Track these basics as facts: from_zip, to_zip, move_date, size_or_items.

Transfer-ready ideally has all four, but if the caller says “yes, connect me” and one is missing, ask just that one (once), then connect.

If a fact fails twice (noisy line), offer to send a text link to confirm that detail.
How to use the spreadsheet each turn

Phase awareness: Start in GREET_COLLECT_CORE. When all 4 basics are filled or rental intent appears, use PERSUADE_TRANSFER. SUPPORT_ROUTING is always available for non-quote topics/DNC/Spanish.

Row selection order (deterministic):

If caller asks about price with missing basics → choose the PRICE_BEFORE_FACTS row.

Else if any of the 4 basics is missing → choose the matching ASK_… row that collects only that fact.

Else if rental/time-pressure triggers → choose a PERSUADE_… row (up to 2 lines), then the ASK_TRANSFER row.

Else if they are ready/agree → choose CONFIRM_TRANSFER.

Else for edge cases (legitimacy, angry, DNC, Spanish, no availability) → choose the appropriate SUPPORT_ROUTING row.

Speak exactly the Micro-Script (1–2 sentences). Do not invent pricing or long explanations.

Execute NextAction:

COLLECT:fieldA|fieldB → ask/store just those fields (don’t re-ask filled ones).

ACTION:offer_live_transfer / send_text_link / transfer_to_dialup / schedule_callback / mark_dnc / crm_update.

BRANCH:if_yes->X|else->Y → follow the caller’s response.

NEXT:TAGCODE → immediately follow the indicated row next turn.

DROP_AFTER_INTRO → after warm intro to mover (~10s), leave unless asked to stay.

END → end politely.
Transfer & warm intro policy

On yes + availability is fine, run offer_live_transfer().

Give a 10-second warm intro: “Hi [Mover], I’m with [Caller]… From [from_zip] to [to_zip], moving on [move_date], [size_or_items]. They’re looking for your best quote today.”

Then drop unless asked to stay.
Guardrails (non-negotiable)

No exact pricing—the mover gives ballpark after connection.

No competitor bashing—frame as side-by-side via live connection.

Don’t repeat questions; ask only what’s missing.

Respect DNC immediately.
ASSISTANT OUTPUT FORMAT (return this JSON each turn)
Always return a compact JSON so our system can act quickly:
"When responding, you must provide your answer in the following JSON format: "
    "{"
    "  \"selected_row\": \"TagID\","
    "  \"say_ssml\": \"<speak>One or two short sentences here.<break time='200ms'/></speak>\","
    "  \"collected_facts\": {},"
    "  \"actions\": [],"
    "  \"next_phase_hint\": \"\","
    "  \"notes\": \"\""
    "}"
    "The say_ssml field should contain 1-2 short sentences in SSML format with natural pauses. "
    "Stop speaking the instant the caller starts talking."
    ""
CSV COLUMNS AND HOW TO USE THEM:

1. Tag / Subtag:
   - Logical identifiers for the conversation step.
   - Subtag refines the Tag (e.g., GRT:PRIMARY).

2. Purpose:
   - Describes WHY this step exists (e.g., greeting, confirm, ask info).

3. Triggers:
   - A list of phrases or keywords (semicolon-separated).
   - If user input matches any trigger, this row becomes eligible.
   - Special triggers:
       • always_on → always valid
       • start / first turn → only for the first message
       • tool_def → system/tool setup (not spoken)
       • hello / hi → greeting triggers

4. RequiredFacts (needed before next):
   - A list of facts that must already be known before using this step.
   - If any required fact is missing, skip this row.

5. Micro-Script or Instruction (EN):
   - EXACT text the AI must say to the user.
   - Do NOT add extra words.
   - Do NOT change wording.
   - Say it exactly as written.

6. NextAction:
   - Defines the NEXT Tag/Subtag to go to or logical pointer.
   - After speaking the Micro-Script, the AI should move to this next step.
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
    
    return base_prompt + dynamic_section + general_rules

# Usage
rules = load_conversation_flow("csvFile.csv")
system_message = build_system_message(rules)
print(system_message)

app = Quart(__name__)
app.secret_key = "ABCDEFGHIJKLMNOPQRST"


# transcript and disposiation api

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
            model="gpt-4o-transcribe",  # or "whisper-1"
            file=f
        )
    return transcript.text

def segment_speakers(transcript_text: str):
    """Ask GPT to split transcript into Agent/Customer speakers"""
    response = openai.chat.completions.create(
        model="gpt-4o-mini",  # lightweight + fast
        messages=[
            {"role": "system", "content": "You are a call transcript formatter."},
            {"role": "user", "content": f"""
            Split this transcript into two speakers: Agent and Customer.
            Keep the order of the conversation, and don't add extra text.
            Keep the correct back-and-forth flow. 
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
        
        # Parse the JSON string to a Python dictionary
        result_dict = json.loads(result_json)
        
        # Return the parsed dictionary (FastAPI will automatically convert to JSON)
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

# Function to update lead information after call
async def update_lead_after_call(lead_id, call_uuid):
    """
    Update lead information after call ends using conversation transcript.
    Only updates if any required fields are missing in the lead record.
    """
    try:
        # 1. Check if lead exists and get current data
        conn = get_db_connection()
        if not conn:
            print("[LEAD_UPDATE] Failed to get database connection")
            return False
            
        cursor = conn.cursor(dictionary=True, buffered=True)
        cursor.execute("SELECT * FROM leads WHERE lead_id = %s", (lead_id,))
        lead = cursor.fetchone()
        cursor.close()
        
        if not lead:
            print(f"[LEAD_UPDATE] Lead {lead_id} not found")
            conn.close()
            return False
            
        # 2. Check for missing fields
        required_fields = {
            'name': lead['name'],
            #'phone': lead['phone'],
            'email': lead['email'],
            'from_city': lead['from_city'],
            'from_state': lead['from_state'],
            'from_zip': lead['from_zip'],
            'to_city': lead['to_city'],
            'to_state': lead['to_state'],
            'to_zip': lead['to_zip'],
            'move_date': lead['move_date'],
            'move_size': lead['move_size']
        }
        
        missing_fields = {field: value for field, value in required_fields.items() if not value or value == ''}
        
        if not missing_fields:
            print(f"[LEAD_UPDATE] Lead {lead_id} has all required fields, no update needed")
            conn.close()
            return True
            
        print(f"[LEAD_UPDATE] Lead {lead_id} has missing fields: {list(missing_fields.keys())}")
        
        # 3. Fetch conversation messages from database
        cursor = conn.cursor(dictionary=True, buffered=True)
        cursor.execute("""
            SELECT speaker, content 
            FROM conversation_messages 
            WHERE conversation_id = %s 
            ORDER BY timestamp ASC
        """, (call_uuid,))
        messages = cursor.fetchall()
        cursor.close()
        conn.close()
        
        if not messages:
            print(f"[LEAD_UPDATE] No messages found for conversation {call_uuid}")
            return False
            
        # 4. Combine messages into a single transcript
        transcript = "\n".join([f"{msg['speaker']}: {msg['content']}" for msg in messages])
        print(f"[LEAD_UPDATE] Transcript length: {len(transcript)} characters")
        
        # 5. Prepare prompt for OpenAI - specify which fields to extract
        prompt = f"""
        You are an AI assistant that extracts customer contact info from phone call transcripts.
        Return JSON with these fields: 
        - name 
        - phone 
        - email 
        - origin_city 
        - origin_state 
        - origin 
        - destination_city 
        - destination_state 
        - destination 
        - move_date (format strictly dd-mm-yyyy) 
        - move_size (return only the numeric bedroom count, e.g., "1" for "one bedroom", "2" for "two-bedroom apartment"). 
        If the size is not a bedroom description, leave move_size as null.  
        Leave missing values as null.


        IMPORTANT: Focus on extracting these missing fields: {', '.join(missing_fields.keys())}

        Transcript:
        "{transcript}"
        """
        
        # 6. Call OpenAI API
        headers = {
            "Authorization": f"Bearer {OPENAI_API_KEY}",
            "Content-Type": "application/json"
        }
        
        async with aiohttp.ClientSession() as session:
            async with session.post(
                "https://api.openai.com/v1/chat/completions",
                headers=headers,
                json={
                    "model": "gpt-4",
                    "messages": [{"role": "user", "content": prompt}],
                    "temperature": 0.2,
                },
                timeout=60
            ) as response:
                if response.status != 200:
                    error_text = await response.text()
                    print(f"[LEAD_UPDATE] OpenAI API error: {response.status} - {error_text}")
                    return False
                    
                result = await response.json()
                gpt_content = result["choices"][0]["message"]["content"]
                print(f"[LEAD_UPDATE] OpenAI response: {gpt_content}")
                
                # 7. Parse JSON response
                try:
                    data = json.loads(gpt_content)
                except json.JSONDecodeError as e:
                    print(f"[LEAD_UPDATE] Failed to parse OpenAI response: {e}")
                    return False
                
                # 8. Extract zip codes from origin and destination
                origin = data.get('origin', '')
                destination = data.get('destination', '')
                from_zip = None
                to_zip = None
                
                if origin:
                    zip_match = re.search(r'\b\d{5}\b', origin)
                    from_zip = zip_match.group(0) if zip_match else None
                    
                if destination:
                    zip_match = re.search(r'\b\d{5}\b', destination)
                    to_zip = zip_match.group(0) if zip_match else None
                
                # 9. Prepare update data - only update fields that were missing
                update_data = {}
                
                if 'name' in missing_fields and data.get('name'):
                    update_data['name'] = data['name']
                if 'phone' in missing_fields and data.get('phone'):
                    update_data['phone'] = data['phone']
                if 'email' in missing_fields and data.get('email'):
                    update_data['email'] = data['email']
                if 'from_city' in missing_fields and data.get('origin_city'):
                    update_data['from_city'] = data['origin_city']
                if 'from_state' in missing_fields and data.get('origin_state'):
                    update_data['from_state'] = data['origin_state']
                if 'from_zip' in missing_fields and from_zip:
                    update_data['from_zip'] = from_zip
                if 'to_city' in missing_fields and data.get('destination_city'):
                    update_data['to_city'] = data['destination_city']
                if 'to_state' in missing_fields and data.get('destination_state'):
                    update_data['to_state'] = data['destination_state']
                if 'to_zip' in missing_fields and to_zip:
                    update_data['to_zip'] = to_zip
                if 'move_date' in missing_fields and data.get('move_date'):
                    update_data['move_date'] = data['move_date']
                if 'move_size' in missing_fields and data.get('move_size'):
                    update_data['move_size'] = data['move_size']
                
                # 10. Update lead record only if we have new data
                if not update_data:
                    print(f"[LEAD_UPDATE] No new data extracted for missing fields")
                    return False
                
                conn = get_db_connection()
                if not conn:
                    print("[LEAD_UPDATE] Failed to get database connection for update")
                    return False
                    
                cursor = conn.cursor()
                
                # Build dynamic update query
                set_clause = ", ".join([f"{field} = %s" for field in update_data.keys()])
                values = list(update_data.values())
                values.append(lead_id)
                
                update_query = f"UPDATE leads SET {set_clause} WHERE lead_id = %s"
                cursor.execute(update_query, values)
                
                conn.commit()
                cursor.close()
                conn.close()
                
                print(f"[LEAD_UPDATE] Updated lead {lead_id} with fields: {list(update_data.keys())}")
                return True
                
    except Exception as e:
        print(f"[LEAD_UPDATE] Error: {e}")
        return False

# Function to hang up call using Plivo API
async def hangup_call(call_uuid, disposition, lead_id, text_message="I have text", followup_datetime=None, from_number=None, to_number=None):
    print("Rajan inside hangup_call")
    if not PLIVO_AUTH_ID or not PLIVO_AUTH_TOKEN:
        print("Plivo credentials not set. Cannot hang up call.")
        return
    
    # Handle disposition 1 (call transfer) differently
    if disposition == 1 or disposition == 9:
        try:
            print(f"[TRANSFER] Starting call transfer for lead_id: {lead_id}")
            
            # Fetch lead data from database
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
            
            print(f"[TRANSFER] Lead Data: {lead_data['phone']}")
            
            # Prepare the payload
            payload = {
            'id': lead_data.get('t_call_id'),
            'action': 1,
            'type': 1,
            'review_call': lead_data.get('review_call',0),  # defaults to 0 if None or missing
            'accept_call': 0,
            'rep_id': lead_data.get('t_rep_id'),
            'logic_check': 1,
            'lead_id': lead_data.get('t_lead_id'),
            'categoryId': 1,
            'buffer_id_arr': '',
            'campaignId': lead_data.get('campaign_id'),
            'campaignScore': lead_data.get('campaign_score'),
            'campaignNumber': lead_data.get('mover_phone'),
            'campaignPayout': lead_data.get('campaign_payout')
            }
            
            print(f"[TRANSFER] Payload: {payload}")
            print(f"[TRANSFER] From Number: {from_number}")
            print(f"[TRANSFER] To Number: {to_number}")
            if to_number == "12176186806":
                # Determine URL based on phone number
                if lead_data['phone'] in ("6025298353", "6263216095", "6266957119"):
                    url = "https://snapit:mysnapit22@zapstage.snapit.software/api/calltransfertest"
                else:
                    url = "https://zapprod:zap2024@zap.snapit.software/api/calltransfertest"

                print(f"[TRANSFER] Using URL: {url}")
            else:
                if lead_data['phone'] in ("6025298353", "6263216095", "6266957119"):
                    url = "https://snapit:mysnapit22@stage.linkup.software/api/calltransfertest"
                else:    
                    print("to_number is not 12176186806")
                    url = "https://linkup:newlink_up34@linkup.software/api/calltransfertest"
                    #url = "https://snapit:mysnapit22@stage.linkup.software/api/calltransfertest"
                # if lead_data['phone'] in ("6025298353", "6263216095"):
                #     url = "https://snapit:mysnapit22@stage.linkup.software/api/calltransfertest"
                # else:
                #     url = "https://zapprod:zap2024@linkup.software/api/calltransfertest"
            
            # Make the API call
            async with aiohttp.ClientSession() as session:
                # ✅ Add 3-second delay before making API call
                await asyncio.sleep(4)
                async with session.post(
                    url,
                    headers={'Content-Type': 'application/json'},
                    data=json.dumps(payload)
                ) as resp:
                    if resp.status == 200:
                        response_text = await resp.text()
                        print(f"[TRANSFER] API call successful: {response_text}")
                        return
                    else:
                        response_text = await resp.text()
                        raise Exception(f"API call failed with status {resp.status}: {response_text}")
        
        except Exception as e:
            print(f"[TRANSFER] Error: {e}")
            # Continue with normal hangup if transfer fails
    
    # Original hangup logic for other dispositions or failed transfers
    # 1️⃣ Hang up the call via Plivo API
    url = f"https://api.plivo.com/v1/Account/{PLIVO_AUTH_ID}/Call/{call_uuid}/"
    auth_string = f"{PLIVO_AUTH_ID}:{PLIVO_AUTH_TOKEN}"
    auth_header = base64.b64encode(auth_string.encode()).decode()
    print(f"[DEBUG] Attempting to hang up call {call_uuid}")
    print(f"[DEBUG] URL: {url}")
    print(f"[DEBUG] Auth header: {auth_header[:5]}...")
    
    if(disposition == 10):
        print(f"[DEBUG] disposition 10 start-timer 1")
        await asyncio.sleep(12)
        print(f"[DEBUG] disposition 10 end-timer 12")
    if(disposition == 11):
        print(f"[DEBUG] disposition 11 start-timer 1")
        await asyncio.sleep(8)
        print(f"[DEBUG] disposition 11 end-timer 8")
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
        "disposition": 6 if disposition in (10, 11) else disposition,
        'to_number': to_number
    }
    if followup_datetime:
        params["followupdatetime"] = followup_datetime
        print(f"[DEBUG] Including followupdatetime: {followup_datetime}")
    # Build the URL with proper encoding
    query_string = urlencode(params, quote_via=quote)
    print(f"[DEBUG] Query string: {query_string}")
    redirect_url = f"http://54.176.128.91/disposition_route?{query_string}"
    response = requests.post(redirect_url)
    print(f"[DEBUG] Redirect URL: {response}")

def get_timezone_from_db(timezone_id: int):
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True, buffered=True)
    cursor.execute("SELECT timezone FROM mst_timezone WHERE timezone_id = %s LIMIT 1", (timezone_id,))
    row = cursor.fetchone()
    cursor.close()
    conn.close()
    if row:
        return row["timezone"]   # e.g., "Asia/Kolkata"
    return None

# Function to check disposition based on user input
def check_disposition(transcript, lead_timezone, ai_agent_name):
    transcript_lower = transcript.lower()
    
    # Pattern 1: Do not call

    if re.search(r"\b(moving specialist|moving agent|moving company|moving assistance|moving representative)\b", transcript_lower):
        return 10, "I'll transfer you to a moving company who can better assist you.", None
        
    elif re.search(r"\b(human)\b", transcript_lower):
        return 9, "I'll transfer you to a human agent who can better assist you.", None

    elif re.search(r"\b(don'?t call|do not call|not to call|take me off)\b", transcript_lower):
        return 2, "No worries, sorry to bother you. Have a great day", None
    
    # Pattern 2: Wrong number
    elif re.search(r"\b(wrong number|invalid number|incorrect number)\b", transcript_lower):
        return 7, "No worries, sorry to bother you. Have a great day", None
    
    # Pattern 3: Not interested
    elif re.search(r"\b(not looking to move|not looking|not interested)\b", transcript_lower):
        return 3, "No worries, sorry to bother you. Have a great day", None
    
    # Pattern 4: Not available
    elif re.search(r"\b(hang up or press|reached the maximum time allowed to make your recording|at the tone|are busy|am busy|busy|call me later|call me|call me at)\b", transcript_lower):
        # Check if it's a busy/call me later pattern that might have a datetime
        if re.search(r"\b(are busy|am busy|busy|call me later|call me|call me at)\b", transcript_lower):
            followup_datetime = get_followup_datetime(transcript_lower, lead_timezone)
            print(f'🎤 Lead Timezone: {lead_timezone}')
            print(f'🎤 Followup DateTime: {followup_datetime}')
            if followup_datetime:
                return 4, "I will call you later. Nice to talk with you. Have a great day.", followup_datetime
        # Default response for voicemail or no datetime found
        return 6, "I will call you later. Nice to talk with you. Have a great day.", None

    elif re.search(r"\b(Cannot accept any messages at this time|trying to reach is unavailable|call you back as soon as possible|automated voice messaging system|please record your message|record your message|voicemail|voice mail|leave your message|please leave the name and number|please leave a name and number|leave me a message|leave a message|recording|leave me your|will get back to you|leave me your|the person you are trying to reach is unavailable|please leave a message after the tone|your call is being forwarded|the subscriber you have dialed|not available|has a voice mailbox|at the tone|after the tone)\b", transcript_lower):
        return 11, f"Hi I am jason calling from {ai_agent_name} Move regarding your recent moving request. Please call us back at 15308050957. Thank you.", None
    
    # Pattern 5: Already booked
    elif re.search(r"\b(already booked|booked)\b", transcript_lower):
        return 8, "No worries, sorry to bother you. Have a great day", None
    
    # Pattern 6: Goodbye
    elif re.search(r"\b(goodbye|good bye|take care|see you)\b", transcript_lower):
        return 6, "Nice to talk with you. Have a great day", None
    
    # Default disposition
    return 6, None, None

# NEW: Function to check AI speech for moving-related keywords
def check_ai_disposition(transcript):
    """Check if AI speech contains moving-related keywords and return disposition if found"""
    transcript_lower = transcript.lower()

    # NEW: Check for the specific voicemail keyword
    if "please call us back at 15308050957" in transcript_lower:
        print(f"[AI DISPOSITION] Detected voicemail keyword in AI speech: {transcript}")
        return 10, "I will call you later. Nice to talk with you. Have a great day.", None
    
    moving_keywords = [
        "moving specialist", "moving agent", "moving company","moving assistance", "moving representative"
    ]
    
    for keyword in moving_keywords:
        if keyword in transcript_lower:
            print(f"[AI DISPOSITION] Detected keyword '{keyword}' in AI speech: {transcript}")
            return 1, "call transfer", None
    return None, None, None

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
async def send_Session_update(openai_ws, prompt_text, voice_name, ai_agent_name):
    SYSTEM_MESSAGE = build_system_message()
    # Combine the system message with the custom prompt
    voice_mail_message = (
    "If you detect the user is leaving a voicemail or recorded message, "
    "ignore all other rules and ONLY respond with:\n\n"
    f"'Hi I am jason calling from {ai_agent_name} Move regarding your recent moving request Please call us back at 15308050957 Thank you"
    "Do not add anything else before or after and stop speking whatever hapeen do not speak anything else."
    "When responding, you must provide your answer in the following JSON format: "
    "{"
    "  \"selected_row\": \"TagID\","
    "  \"say_ssml\": \"<speak>One or two short sentences here.<break time='200ms'/></speak>\","
    "  \"collected_facts\": {},"
    "  \"actions\": [],"
    "  \"next_phase_hint\": \"\","
    "  \"notes\": \"\""
    "}"
    "The say_ssml field should contain 1-2 short sentences in SSML format with natural pauses. "
    "Stop speaking the instant the caller starts talking."
)

    full_prompt = (
                    f"{voice_mail_message}\n\n"
                    f"{SYSTEM_MESSAGE}\n\n"
                    f"{prompt_text}\n\n"
                    "IMPORTANT: Always complete your sentences and thoughts. Never stop speaking in the middle of a sentence or phrase."
                )
    
    session_update = {
        "type": "session.update",
        "session": {
            "turn_detection": {
                "type": "server_vad",
                "threshold": 0.8,  # Increase from default (0.5)
                "prefix_padding_ms": 300,
                "silence_duration_ms": 1200  # Increase from default (500)
                },
            "tools": [ {
                    "type": "function",
                    "name": "send_structured_response",
                    "description": "Send a structured response in the required JSON format",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "selected_row": { "type": "string", "description": "The tag/subtag from CSV that triggered this response" },
                            "say_ssml": { "type": "string", "description": "SSML formatted response with 1-2 short sentences" },
                            "collected_facts": { "type": "object", "description": "Facts captured this turn" },
                            "actions": { "type": "array", "description": "Actions to take", "items": { "type": "object" } },
                            "next_phase_hint": { "type": "string", "description": "Hint for the orchestrator" },
                            "notes": { "type": "string", "description": "Short rationale or branch" }
                        },
                        "required": ["selected_row", "say_ssml"]
                    }
                }],
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
    session["call_uuid"] = call_uuid

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
                        audio_message = f"HI {ai_agent_name}. how are you today?"
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
                # Determine which URL to use based on phone number
                if lead_data['phone'] in ("6025298353", "6263216095", "6266957119"):
                    url = "https://snapit:mysnapit22@zapstage.snapit.software/api/calltransfertest"
                else:
                    url = "https://zapprod:zap2024@zap.snapit.software/api/calltransfertest"
            else:
                if lead_data['phone'] in ("6025298353", "6263216095", "6266957119"):
                    url = "https://snapit:mysnapit22@stage.linkup.software/api/calltransfertest"
                else:    
                    print("to_number is not 12176186806")
                    url = "https://linkup:newlink_up34@linkup.software/api/calltransfertest"
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
    lead_timezone = websocket.args.get('lead_timezone', 'unknown')
    
    
    print('audio_message', audio_message)
    print('voice_name', voice_name)
    print('ai_agent_id', ai_agent_id)
    print('lead_timezone', lead_timezone)
    print('ai_agent_name', ai_agent_name)
    
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
        'disposition_hangup_scheduled': False,  # Flag to prevent multiple hangup attempts
        'expecting_user_response': False,  # NEW: Flag to indicate we're expecting a user response
        'transfer_initiated': False,  # NEW: Flag to prevent multiple transfer attempts
        'lead_update_scheduled': False,  # Flag to prevent multiple lead updates
        'ai_finished_speaking_time': 0,  # NEW: Timestamp when AI audio finished
    }
    
    # Fetch prompt_text from database using ai_agent_id
    prompt_text = build_system_message()  # Default to system message
    if ai_agent_id:
        conn = get_db_connection()
        if conn:
            try:
                cursor = conn.cursor(dictionary=True, buffered=True)
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
    prompt_to_use = f"{build_system_message()}\n\n{prompt_text}"
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
            conversation_state['expecting_user_response'] = False  # Reset flag
            # Reset conversation state to ensure a clean response
            conversation_state['in_ai_response'] = False
            conversation_state['current_ai_text'] = ''
            
            # Check if we've reached the maximum attempts
            if conversation_state['are_you_there_count'] >= conversation_state['max_are_you_there']:
                print(f"[TIMEOUT] Reached maximum 'Are you there?' attempts ({conversation_state['max_are_you_there']}), disconnecting call")
                # Set disposition for no response and prepare to hang up
                conversation_state['disposition'] = 6  # Default disposition for no response
                conversation_state['disposition_message'] = "Thank you for your time. Have a great day."
                
                # Schedule lead update if not already done
                if not conversation_state.get('lead_update_scheduled', False) and conversation_state['lead_id'] and conversation_state['lead_id'] != 'unknown' and conversation_state['lead_id'] != 0:
                    print(f"[LEAD_UPDATE] Scheduling lead update for lead_id {conversation_state['lead_id']} after call {conversation_state['conversation_id']}")
                    asyncio.create_task(update_lead_after_call(conversation_state['lead_id'], conversation_state['conversation_id']))
                    conversation_state['lead_update_scheduled'] = True
                
                # Create final goodbye message before hanging up
                goodbye_response = {
                    "type": "response.create",
                    "response": {
                        "modalities": ["text", "audio"],
                        "temperature": 0.6,  # Set to minimum allowed temperature
                        "instructions": "Say exactly: 'Thank you. Have a great day.' and then end the conversation."
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
                print(f"Rajan1")
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
            conversation_state['expecting_user_response'] = True  # We're now expecting a response
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
            not conversation_state.get('call_ending', False) and
            not conversation_state.get('transfer_initiated', False)):  # Check if transfer is in progress
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
        conversation_state['expecting_user_response'] = False  # Reset flag
        
    url = "wss://api.openai.com/v1/realtime?model=gpt-4o-realtime-preview-2024-10-01"
    headers = {
        "Authorization": f"Bearer {OPENAI_API_KEY}",
        "OpenAI-Beta": "realtime=v1",
    }
    
    try: 
        async with websockets.connect(url, extra_headers=headers) as openai_ws:
            print('Connected to the OpenAI Realtime API')
            
            # Send session update first
            await send_Session_update(openai_ws, prompt_to_use, voice_name, ai_agent_name)
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
            
            receive_task = asyncio.create_task(receive_from_plivo(plivo_ws, openai_ws,conversation_state))
            
            async for message in openai_ws:
                await receive_from_openai(message, plivo_ws, openai_ws, conversation_state, handle_timeout, ai_agent_name)
            
            await receive_task
    
    except asyncio.CancelledError:
        print('Client disconnected')
    except websockets.ConnectionClosed:
        print("Connection closed by OpenAI server")
    except Exception as e:
        print(f"Error during OpenAI's websocket communication: {e}")

async def receive_from_plivo(plivo_ws, openai_ws,conversation_state):
    try:
        while True:
            message = await plivo_ws.receive()
            data = json.loads(message)
            if data['event'] == 'media' and openai_ws.open:

                 # 🔑 Prevent AI echo by skipping audio while AI is speaking
                if conversation_state.get("in_ai_response", False):
                    continue  # Ignore this audio frame

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


async def receive_from_openai(message, plivo_ws, openai_ws, conversation_state, handle_timeout, ai_agent_name):
    try:
        response = json.loads(message)
        event_type = response.get('type', 'unknown')
        
        # Log all event types for debugging (except audio deltas to reduce noise)
        if event_type not in ['response.audio.delta', 'response.audio.done']:
            
            # Extract the text from the response
            text_content = response.get('delta', response.get('text', ''))
            
            print("\n" + "="*50)
            print("FULL OPENAI JSON RESPONSE:")
            print(json.dumps(response, indent=2))
            print("="*50 + "\n")
        
        print('response received from OpenAI Realtime API: ', response['type'])
        
        

        # SIMPLE: Just add AI activity events to the same timeout cancellation logic
        # that you already have for user events
        activity_events = [
            # Existing user events (keep these as-is)
            'input_audio_buffer.speech_started',
            'conversation.item.input_audio_transcription.completed',
            # NEW: Add AI events
            'response.created',
            'response.text.delta', 
            'response.audio.delta',
            'response.audio_transcript.delta'
        ]
        
        if event_type in activity_events:
            # Define source INSIDE the conditional where it's used
            source = "AI" if event_type.startswith('response.') else "User"
            # IMPORTANT: Don't reset timer if this is an "Are you there?" response
            if event_type.startswith('response.') and conversation_state.get('is_are_you_there_response', False):
                print(f"[TIMEOUT] NOT resetting timer - this is an 'Are you there?' response")
                # Don't reset the timer for "Are you there?" responses
                pass
            else:
                # Reset timer for normal AI responses and all user activity
                if conversation_state['timeout_task'] and not conversation_state['timeout_task'].done():
                    conversation_state['timeout_task'].cancel()
                    print(f"[TIMEOUT] Timer cancelled - {source} activity ({event_type})")
                else:
                    print(f"[TIMEOUT] Timer cancel requested - {source} activity ({event_type})")
            conversation_state['waiting_for_user'] = False
            conversation_state['timeout_task'] = None
            conversation_state['expecting_user_response'] = False
            
            # Reset "Are you there?" counter for any activity
            if source == "User" and conversation_state['are_you_there_count'] > 0:
                print(f"[TIMEOUT] Resetting 'Are you there?' counter due to {source} activity")
                conversation_state['are_you_there_count'] = 0

        # Handle AI text responses
        if event_type == 'response.text.delta':
            # If this is a disposition response, handle it specially
            if conversation_state.get('is_disposition_response', False):
                delta = response.get('delta', '')
                print(f"[AI Disposition] {delta}", end='', flush=True)
                conversation_state['disposition_text'] = conversation_state.get('disposition_text', '') + delta
                if text_content.strip().startswith('{') and text_content.strip().endswith('}'):
                    json_response = json.loads(text_content)
                    process_structured_response(json_response, plivo_ws)
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

            # Check if this was an "Are you there?" response
            text = response.get('text', '') or conversation_state['current_ai_text']

            if text_content.strip().startswith('{') and text_content.strip().endswith('}'):
                json_response = json.loads(text_content)
                process_structured_response(json_response, plivo_ws)
                return
            
            if text and "are you there" in text.lower():
                print(f"[DEBUG] Detected 'Are you there?' response: {text}")
                conversation_state['is_are_you_there_response'] = True
            else:
                conversation_state['is_are_you_there_response'] = False

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
            final_ai_text = text or conversation_state['current_ai_text']

            # --- Store the AI's last sentence for echo detection ---
            if final_ai_text:
                # Split by common sentence terminators and get the last part
                sentences = re.split(r'[.!?]', final_ai_text)
                # Filter out empty strings and strip whitespace
                last_sentence = next((s.strip() for s in reversed(sentences) if s.strip()), '')
                if last_sentence:
                    conversation_state['last_ai_sentence'] = last_sentence
                    print(f"[DEBUG] Stored last AI sentence for echo check: '{last_sentence}'")
            # --- END OF NEW CODE ---

            if final_ai_text:
                print(f"[LOG] AI Text Response: {final_ai_text}")
                # Check if the response ends with a sentence terminator
                if not any(final_ai_text.endswith(punct) for punct in ['.', '!', '?']):
                    print(f"[WARNING] AI response may be incomplete: {final_ai_text}")
                    # If the response seems incomplete, create a new response to complete it
                    if not conversation_state.get('is_disposition_response', False):
                        completion_prompt = f"Please complete your previous thought: '{final_ai_text}'"
                        await create_response_with_completion_instructions(openai_ws, completion_prompt)
                        conversation_state['active_response'] = True
                
                # Log to database
                await log_conversation(
                    conversation_state['lead_id'],
                    conversation_state['conversation_id'],
                    'assistant',
                    final_ai_text
                )
                
                # Check if we're expecting a user response
                if final_ai_text.endswith('?') or any(phrase in final_ai_text.lower() for phrase in [
                    'how are you', 'am i speaking to', 'can you tell me', 'do you need', 
                    'would you like', 'are you interested', 'what is your', 'when would you',
                    'where are you', 'why are you', 'which one', 'how many', 'how much',
                    'can i help', 'may i help', 'what can i', 'how can i', 'is there',
                    'are there', 'do you have', 'have you', 'did you', 'will you'
                ]):
                    conversation_state['expecting_user_response'] = True
                    print(f"[DEBUG] AI asked a question, expecting user response")
                else:
                    conversation_state['expecting_user_response'] = False
                    print(f"[DEBUG] AI made a statement, not expecting immediate response")
                
                # Check AI speech for moving-related keywords
                if not conversation_state.get('is_disposition_response', False) and not conversation_state.get('transfer_initiated', False):
                    disposition, disposition_message, followup_datetime = check_ai_disposition(final_ai_text)
                    if disposition:
                        print(f"[LOG] AI triggered disposition {disposition}: {disposition_message}")
                        conversation_state['disposition'] = disposition
                        conversation_state['disposition_message'] = disposition_message
                        conversation_state['is_disposition_response'] = True
                        conversation_state['disposition_response_id'] = response.get('response_id', '')
                        
                        # Schedule lead update if not already done
                        if not conversation_state.get('lead_update_scheduled', False) and conversation_state['lead_id'] and conversation_state['lead_id'] != 'unknown' and conversation_state['lead_id'] != 0:
                            print(f"[LEAD_UPDATE] Scheduling lead update for lead_id {conversation_state['lead_id']} after call {conversation_state['conversation_id']}")
                            asyncio.create_task(update_lead_after_call(conversation_state['lead_id'], conversation_state['conversation_id']))
                            conversation_state['lead_update_scheduled'] = True
                        
                        # IMMEDIATE TRANSFER: If disposition is 1 (transfer), trigger immediately
                        if disposition == 1 or disposition == 9 or disposition == 10:
                            print("[TRANSFER] Immediately initiating call transfer")
                            conversation_state['transfer_initiated'] = True
                            
                            # Cancel any active response
                            if conversation_state['active_response']:
                                cancel_response = {"type": "response.cancel"}
                                print(f"Rajan2")
                                await openai_ws.send(json.dumps(cancel_response))
                                conversation_state['active_response'] = False
                            
                            # Immediately trigger the transfer
                            await hangup_call(
                                conversation_state['conversation_id'], 
                                conversation_state['disposition'], 
                                conversation_state['lead_id'],
                                conversation_state.get('disposition_message', ''),
                                followup_datetime=conversation_state.get('followup_datetime'),
                                from_number=conversation_state.get('from_number'),
                                to_number=conversation_state.get('to_number')
                            )
                            return
                        else:
                            # For other dispositions, mark that the call is ending
                            conversation_state['call_ending'] = True
                
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


            # Check if this was an "Are you there?" response from audio transcript
            transcript = response.get('transcript', '') or conversation_state['ai_transcript']
            if transcript and "are you there" in transcript.lower():
                print(f"[DEBUG] Detected 'Are you there?' audio transcript: {transcript}")
                conversation_state['is_are_you_there_response'] = True
            else:
                conversation_state['is_are_you_there_response'] = False


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
                
                # Check if we're expecting a user response
                if transcript.endswith('?') or any(phrase in transcript.lower() for phrase in [
                    'how are you', 'am i speaking to', 'can you tell me', 'do you need', 
                    'would you like', 'are you interested', 'what is your', 'when would you',
                    'where are you', 'why are you', 'which one', 'how many', 'how much',
                    'can i help', 'may i help', 'what can i', 'how can i', 'is there',
                    'are there', 'do you have', 'have you', 'did you', 'will you'
                ]):
                    conversation_state['expecting_user_response'] = True
                    print(f"[DEBUG] AI asked a question, expecting user response")
                else:
                    conversation_state['expecting_user_response'] = False
                    print(f"[DEBUG] AI made a statement, not expecting immediate response")
                
                # Check AI speech for moving-related keywords
                if not conversation_state.get('is_disposition_response', False) and not conversation_state.get('transfer_initiated', False):
                    disposition, disposition_message, followup_datetime = check_ai_disposition(transcript)
                    if disposition:
                        print(f"[LOG] AI triggered disposition {disposition}: {disposition_message}")
                        conversation_state['disposition'] = disposition
                        conversation_state['disposition_message'] = disposition_message
                        conversation_state['is_disposition_response'] = True
                        conversation_state['disposition_response_id'] = response.get('response_id', '')
                        
                        # Schedule lead update if not already done
                        if not conversation_state.get('lead_update_scheduled', False) and conversation_state['lead_id'] and conversation_state['lead_id'] != 'unknown' and conversation_state['lead_id'] != 0:
                            print(f"[LEAD_UPDATE] Scheduling lead update for lead_id {conversation_state['lead_id']} after call {conversation_state['conversation_id']}")
                            asyncio.create_task(update_lead_after_call(conversation_state['lead_id'], conversation_state['conversation_id']))
                            conversation_state['lead_update_scheduled'] = True
                        
                        # IMMEDIATE TRANSFER: If disposition is 1 (transfer), trigger immediately
                        if disposition == 1 or disposition == 9 or disposition == 10:
                            print("[TRANSFER] Immediately initiating call transfer")
                            conversation_state['transfer_initiated'] = True
                            
                            # Cancel any active response
                            if conversation_state['active_response']:
                                cancel_response = {"type": "response.cancel"}
                                print(f"Rajan4")
                                await openai_ws.send(json.dumps(cancel_response))
                                conversation_state['active_response'] = False
                            
                            # Immediately trigger the transfer
                            await hangup_call(
                                conversation_state['conversation_id'], 
                                conversation_state['disposition'], 
                                conversation_state['lead_id'],
                                conversation_state.get('disposition_message', ''),
                                followup_datetime=conversation_state.get('followup_datetime'),
                                from_number=conversation_state.get('from_number'),
                                to_number=conversation_state.get('to_number')
                            )
                            return
                        else:
                            # For other dispositions, mark that the call is ending
                            conversation_state['call_ending'] = True
            else:
                print(f"[LOG] AI Audio Transcript: {conversation_state['ai_transcript']}")
                # Log to database
                await log_conversation(
                    conversation_state['lead_id'],
                    conversation_state['conversation_id'],
                    'assistant',
                    conversation_state['ai_transcript']
                )
                
                # Check if we're expecting a user response
                if conversation_state['ai_transcript'].endswith('?') or any(phrase in conversation_state['ai_transcript'].lower() for phrase in [
                    'how are you', 'am i speaking to', 'can you tell me', 'do you need', 
                    'would you like', 'are you interested', 'what is your', 'when would you',
                    'where are you', 'why are you', 'which one', 'how many', 'how much',
                    'can i help', 'may i help', 'what can i', 'how can i', 'is there',
                    'are there', 'do you have', 'have you', 'did you', 'will you'
                ]):
                    conversation_state['expecting_user_response'] = True
                    print(f"[DEBUG] AI asked a question, expecting user response")
                else:
                    conversation_state['expecting_user_response'] = False
                    print(f"[DEBUG] AI made a statement, not expecting immediate response")
                
                # Check AI speech for moving-related keywords
                if not conversation_state.get('is_disposition_response', False) and not conversation_state.get('transfer_initiated', False):
                    disposition, disposition_message, followup_datetime = check_ai_disposition(conversation_state['ai_transcript'])
                    if disposition:
                        print(f"[LOG] AI triggered disposition {disposition}: {disposition_message}")
                        conversation_state['disposition'] = disposition
                        conversation_state['disposition_message'] = disposition_message
                        conversation_state['is_disposition_response'] = True
                        conversation_state['disposition_response_id'] = response.get('response_id', '')
                        
                        # Schedule lead update if not already done
                        if not conversation_state.get('lead_update_scheduled', False) and conversation_state['lead_id'] and conversation_state['lead_id'] != 'unknown' and conversation_state['lead_id'] != 0:
                            print(f"[LEAD_UPDATE] Scheduling lead update for lead_id {conversation_state['lead_id']} after call {conversation_state['conversation_id']}")
                            asyncio.create_task(update_lead_after_call(conversation_state['lead_id'], conversation_state['conversation_id']))
                            conversation_state['lead_update_scheduled'] = True
                        
                        # IMMEDIATE TRANSFER: If disposition is 1 (transfer), trigger immediately
                        if disposition == 1 or disposition == 9 or disposition == 10:
                            print("[TRANSFER] Immediately initiating call transfer")
                            conversation_state['transfer_initiated'] = True
                            
                            # Cancel any active response
                            if conversation_state['active_response']:
                                cancel_response = {"type": "response.cancel"}
                                print(f"Rajan5")
                                await openai_ws.send(json.dumps(cancel_response))
                                conversation_state['active_response'] = False
                            
                            # Immediately trigger the transfer
                            await hangup_call(
                                conversation_state['conversation_id'], 
                                conversation_state['disposition'], 
                                conversation_state['lead_id'],
                                conversation_state.get('disposition_message', ''),
                                followup_datetime=conversation_state.get('followup_datetime'),
                                from_number=conversation_state.get('from_number'),
                                to_number=conversation_state.get('to_number')
                            )
                            return
                        else:
                            # For other dispositions, mark that the call is ending
                            conversation_state['call_ending'] = True
            conversation_state['ai_transcript'] = ''
            
        # Handle response creation
        elif event_type == 'response.created':
            response_id = response.get('response', {}).get('id', '')
            print(f"[LOG] Response created with ID: {response_id}")
            
            # Check if this response is for an ignored input
            if conversation_state.get('last_input_ignored', False):
                print("[LOG] Cancelling response for ignored input")
                cancel_response = {"type": "response.cancel"}
                print(f"Rajan6")
                #await openai_ws.send(json.dumps(cancel_response))
                conversation_state['active_response'] = False
                conversation_state['last_input_ignored'] = False
                return
            
            # If this is a disposition response, mark it
            if conversation_state.get('is_disposition_response', False):
                print(f"[DEBUG] This is a disposition response, ID: {response_id}")
                conversation_state['disposition_response_id'] = response_id
            
            conversation_state['active_response'] = True
            
        # Handle response completion
        elif event_type == 'response.done':

             # Reset the "Are you there?" flag when response is completely done
            if conversation_state.get('is_are_you_there_response', False):
                print(f"[DEBUG] 'Are you there?' response completed")

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
                    followup_datetime=conversation_state.get('followup_datetime'),
                    from_number=conversation_state.get('from_number'),
                    to_number=conversation_state.get('to_number')
                )
                conversation_state['pending_hangup'] = False
            
        # Handle user transcriptions
        elif event_type == 'conversation.item.input_audio_transcription.completed':
            # Reset the ignored input flag at the start of each new input
            conversation_state['last_input_ignored'] = False
            
            # If the call is ending or transfer is initiated, ignore any more input
            if conversation_state.get('call_ending', False) or conversation_state.get('transfer_initiated', False):
                print("[LOG] Call is ending or transfer initiated, ignoring user input")
                return
            
            transcript = response.get('transcript', '').strip()
            
            # --- NEW: Time-Based Echo Detection ---
            import time
            current_time = time.time()
            ai_finished_time = conversation_state.get('ai_finished_speaking_time', 0)
            time_since_ai_spoke = current_time - ai_finished_time

            # List of words that are likely echoes if heard immediately after AI speaks
            immediate_echo_noise = {"bye", "okay", "ok", "yes", "no", "thanks", "thank you", "alright"}

            if time_since_ai_spoke < 1.5 and transcript.lower() in immediate_echo_noise:
                print(f"[LOG] 🚫 Discarding likely immediate echo: '{transcript}' (heard {time_since_ai_spoke:.2f}s after AI)")
                conversation_state['last_input_ignored'] = True
                return
            # --- END OF NEW CODE ---

            # --- Echo Detection Check ---
            last_ai_sentence = conversation_state.get('last_ai_sentence', '')
            if last_ai_sentence:
                similarity = difflib.SequenceMatcher(None, transcript.lower(), last_ai_sentence.lower()).ratio()
                print(f"[DEBUG] Echo check: User='{transcript}' vs AI='{last_ai_sentence}' -> Similarity: {similarity:.2f}")
                # If similarity is high, it's likely an echo. Discard it.
                if similarity > 0.8:  # 80% similarity threshold
                    print(f"[LOG] 🚫 Discarding input due to high similarity (echo): '{transcript}'")
                    conversation_state['last_input_ignored'] = True
                    return
            # --- END OF ECHO CHECK ---
            
            # --- Strengthened Noise Filters ---
            cleaned_transcript = re.sub(r'[^a-zA-Z\s]', '', transcript).strip().lower()
            if not cleaned_transcript or len(cleaned_transcript) < 2:
                print(f"[LOG] Ignored empty/short or non-alphabetic transcript: '{transcript}'")
                conversation_state['last_input_ignored'] = True
                return

            false_positives = {
                "yeah", "okay","I've been...", "ok", "hmm", "um", "uh", "hi", "test", "testing", "thank you", "Thank you","Bye.", "Bye", "Bye.", "Bye-bye.", "bye-bye", "bye-bye-bye", "thanks", "Much", "All right.", "Yes.", "Thank you.", "Same here.", "Good evening.", "You", "hello", "hey"
            }
            if cleaned_transcript in false_positives:
                print(f"[LOG] Ignored false positive: '{transcript}'")
                conversation_state['last_input_ignored'] = True
                return
            # --- END OF NOISE FILTER ---

            if response.get("confidence", 1.0) < 0.85:
                print(f"[LOG] Ignored low-confidence transcript")
                conversation_state['last_input_ignored'] = True
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
                conversation_state['expecting_user_response'] = False  # Reset flag
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
            disposition, disposition_message, followup_datetime = check_disposition(transcript, conversation_state['lead_timezone'], ai_agent_name)
            if disposition_message:  # Only process if disposition message is not None
                print(f"[LOG] Detected disposition {disposition}: {disposition_message}")
                conversation_state['disposition'] = disposition
                conversation_state['disposition_message'] = disposition_message
                if followup_datetime:
                    print(f"[LOG] Detected follow-up datetime: {followup_datetime}")
                    conversation_state['followup_datetime'] = followup_datetime
                
                # Mark that the call is ending
                conversation_state['call_ending'] = True
                
                # Schedule lead update if not already done
                if not conversation_state.get('lead_update_scheduled', False) and conversation_state['lead_id'] and conversation_state['lead_id'] != 'unknown' and conversation_state['lead_id'] != 0:
                    print(f"[LEAD_UPDATE] Scheduling lead update for lead_id {conversation_state['lead_id']} after call {conversation_state['conversation_id']}")
                    asyncio.create_task(update_lead_after_call(conversation_state['lead_id'], conversation_state['conversation_id']))
                    #conversation_state['lead_update_scheduled'] = True
                
                # Cancel any active response
                if conversation_state.get('active_response', True):
                    print("[LOG] Cancelling active response for disposition message")
                    print(f"Rajan7")
                    cancel_response = {
                        "type": "response.cancel"
                    }
                    await openai_ws.send(json.dumps(cancel_response))
                    conversation_state['active_response'] = False
                #disposition_message = '!'
                # Create a response that will speak the disposition message
                # create_response = {
                #     "type": "response.create",
                #     "response": {
                #         "modalities": ["text", "audio"],
                #         "temperature": 0.7,
                #         "instructions": f"Say exactly: '{disposition_message}'"
                #     }
                # }
                #if(disposition == 11):
                #await openai_ws.send(json.dumps(create_response))
                conversation_state['active_response'] = True
                conversation_state['is_disposition_response'] = True
                conversation_state['disposition_response_id'] = None  # Will be set when response.created is received
                conversation_state['disposition_audio_sent'] = False  # Track if disposition audio has been sent
                print(f"[DEBUG] Set is_disposition_response to True and created disposition response")

                print(f"[LOG] Directly hanging up call with disposition {disposition}")
                await hangup_call(
                    conversation_state['conversation_id'], 
                    conversation_state['disposition'], 
                    conversation_state['lead_id'],
                    conversation_state.get('disposition_message', ''),
                    followup_datetime=conversation_state.get('followup_datetime'),
                    from_number=conversation_state.get('from_number'),
                    to_number=conversation_state.get('to_number')
                )
            
            # Check if user is speaking in a language other than English
            elif any(ord(char) > 127 for char in transcript):  # Check for non-ASCII characters
                print("[LOG] Non-English detected")
                
                # If there's an active response, cancel it and set a flag to send reminder later
                if conversation_state['active_response']:
                    print("[LOG] Cancelling active response to send language reminder")
                    print(f"Rajan8")
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
                conversation_state['expecting_user_response'] = False  # Reset flag
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
                
        # Handle speech stopped
        elif event_type == 'input_audio_buffer.speech_stopped':
            print("[TIMEOUT] User stopped speaking")
           # Only commit if we actually received audio
            if conversation_state.get("audio_duration_ms", 0) > 100:
                await openai_ws.send(json.dumps({ "type": "input_audio_buffer.commit" }))
                await openai_ws.send(json.dumps({ "type": "input_audio_buffer.clear" }))
            else:
                print("[DEBUG] Skipping commit: not enough audio captured")
                await openai_ws.send(json.dumps({ "type": "input_audio_buffer.clear" }))
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
                # --- NEW: Record the time when AI audio finishes ---
                import time
                conversation_state['ai_finished_speaking_time'] = time.time()
                print(f"[DEBUG] AI finished speaking at: {conversation_state['ai_finished_speaking_time']}")
                # --- END OF NEW CODE ---

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
                        
                         # Check if this was an "Are you there?" response
                        was_are_you_there = conversation_state.get('is_are_you_there_response', False)
                        
                        # Reset the flag now that audio is done
                        conversation_state['is_are_you_there_response'] = False

                        # Only start timeout for user response if we're not in a special state
                        if (not conversation_state.get('is_disposition_response', False) and
                            not conversation_state.get('transfer_initiated', False) and
                            not conversation_state.get('pending_hangup', False) and
                            not conversation_state.get('waiting_for_user', False) and
                            not conversation_state.get('call_ending', False)):
                            
                            # Check if this was an "Are you there?" response
                            if was_are_you_there:
                                print("[TIMEOUT] 'Are you there?' completed - restarting timeout timer")
                            else:
                                print("[TIMEOUT] Normal AI response completed - starting timeout timer")
                            
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
                        else:
                            print("[DEBUG] Not starting timeout timer - in special state")
                    except Exception as e:
                        print(f"[ERROR] Error in audio completion handler: {e}")
                
                asyncio.create_task(handle_audio_completion())
                
                # If this is a disposition response, handle it specially
                if conversation_state.get('is_disposition_response', False):
                    print(f"[LOG] Disposition audio completed, scheduling hangup")
                    
                    # Only schedule hangup if not already scheduled and not a transfer
                    if not conversation_state.get('disposition_hangup_scheduled', False):  
                        #and conversation_state.get('disposition') != 1):  # Don't hangup for transfers
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
                                    followup_datetime=conversation_state.get('followup_datetime'),
                                    from_number=conversation_state.get('from_number'),
                                    to_number=conversation_state.get('to_number')
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


def process_structured_response(json_response, plivo_ws):
    """Process the structured JSON response from OpenAI and send appropriate audio to Plivo"""
    try:
        # Extract the SSML content
        say_ssml = json_response.get('say_ssml', '')
        if say_ssml:
            # For now, we'll just print the structured response
            # In a real implementation, you would convert SSML to audio and send it to Plivo
            print("\n" + "="*50)
            print("STRUCTURED RESPONSE FROM OPENAI:")
            print(json.dumps(json_response, indent=2))
            print("="*50 + "\n")
            
            # Log the collected facts
            if 'collected_facts' in json_response and json_response['collected_facts']:
                print("COLLECTED FACTS:", json.dumps(json_response['collected_facts'], indent=2))
            
            # Log any actions
            if 'actions' in json_response and json_response['actions']:
                print("ACTIONS:", json.dumps(json_response['actions'], indent=2))
                
            # Log the next phase hint
            if 'next_phase_hint' in json_response and json_response['next_phase_hint']:
                print("NEXT PHASE HINT:", json_response['next_phase_hint'])
                
            # Log the notes
            if 'notes' in json_response and json_response['notes']:
                print("NOTES:", json_response['notes'])
    except Exception as e:
        print(f"Error processing structured response: {e}")

    
if __name__ == "__main__":
    print('Starting server to handle inbound Plivo calls...')
    initialize_database()
    app.run(host='0.0.0.0', port=PORT)