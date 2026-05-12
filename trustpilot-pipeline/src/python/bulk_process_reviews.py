import csv
import os
import time
import vertexai
from vertexai.generative_models import GenerativeModel

# Point to the GCP credentials file explicitly
os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = "gcp_credentials.json"

import json
try:
    with open("gcp_credentials.json", "r") as f:
        creds = json.load(f)
        project_id = creds["project_id"]
except Exception as e:
    print(f"Error loading credentials: {e}")
    exit(1)

print("Initializing Vertex AI...")
vertexai.init(project=project_id, location="us-central1")
model = GenerativeModel(model_name="gemini-2.5-pro")

def get_vertex_suggestion(comment, rating):
    if not comment or len(str(comment).strip()) < 5:
        return ""
        
    prompt = (f"A customer left a {rating}-star review for our hairpiece company (Superhairpieces) "
              f"with the following comment:\n\"{comment}\"\n\n"
              "Please provide a highly insightful, actionable COMPANY IMPROVEMENT SUGGESTION (1-2 sentences) for our internal team. "
              "Focus on logistics, QA, training, or product design. If the review is highly positive and fully satisfied with no actionable complaint, return exactly 'NONE'.")
    
    try:
        response = model.generate_content(prompt)
        text = response.text.strip().replace("\n", " ")
        if text == "NONE" or text.startswith("NONE"):
            return ""
        return text
    except Exception as e:
        print(f"Error during API call: {e}")
        time.sleep(5)
        return ""

def main():
    input_file = 'trustpilot_historical_reviews.csv'
    output_file = 'trustpilot_historical_reviews_updated.csv'
    
    if not os.path.exists(input_file):
        print(f"File {input_file} not found.")
        return
        
    print("Reading existing reviews...")
    rows = []
    with open(input_file, 'r', encoding='utf-8') as f:
        reader = csv.DictReader(f)
        fieldnames = reader.fieldnames
        if 'AI Suggestion' not in fieldnames:
            fieldnames.append('AI Suggestion')
        for row in reader:
            rows.append(row)
            
    print(f"Found {len(rows)} reviews. Processing with Vertex AI...")
    
    with open(output_file, 'w', encoding='utf-8', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        
        for i, row in enumerate(rows):
            name = row.get('Name', '')
            rating = row.get('Rating', '')
            comment = row.get('Comment', '')
            
            print(f"Processing [{i+1}/{len(rows)}]: {name} ({rating}-star)")
            suggestion = get_vertex_suggestion(comment, rating)
            row['AI Suggestion'] = suggestion
            
            writer.writerow(row)
            f.flush()
            
    print(f"Successfully wrote to {output_file}.")

if __name__ == '__main__':
    main()
