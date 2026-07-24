import os
import json
import sys

sys.stdout.reconfigure(encoding='utf-8')

protector_cid = "4c499dc6-d09a-4bd0-86f0-cf25988377cd"
transcript_path = rf"C:\Users\ROG\.gemini\antigravity\brain\{protector_cid}\.system_generated\logs\transcript_full.jsonl"
if not os.path.exists(transcript_path):
    transcript_path = rf"C:\Users\ROG\.gemini\antigravity\brain\{protector_cid}\.system_generated\logs\transcript.jsonl"

print(f"Reading protector transcript: {transcript_path}")

if os.path.exists(transcript_path):
    with open(transcript_path, "r", encoding="utf-8") as f:
        lines = f.readlines()
    print(f"Total lines: {len(lines)}")
    for idx, line in enumerate(lines):
        try:
            obj = json.loads(line)
            msg_type = obj.get('type')
            sender = obj.get('sender', 'Unknown')
            recipient = obj.get('recipient', 'Unknown')
            content = obj.get('content', obj.get('message', ''))
            if not content and 'Arguments' in obj:
                content = str(obj.get('Arguments'))
            elif not content and 'output' in obj:
                content = str(obj.get('output'))[:300]
                
            print(f"Line {idx} | Type: {msg_type} | Sender: {sender} | Recipient: {recipient}")
            print(f"  Content: {str(content)[:800].strip()}")
            print("-" * 50)
        except Exception as e:
            pass
else:
    print("Protector transcript not found!")
