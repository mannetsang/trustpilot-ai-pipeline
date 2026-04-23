// Replace this with the URL you saved in your .env file
const GCHAT_WEBHOOK_URL = "https://chat.googleapis.com/v1/spaces/AAQAuV4a-90/messages?key=AIzaSyDdI0hCZtE6vySjMm-WEfRq3CPzqKqqsHI&token=wnW4E3haRHrHa8E95dXl4IhJsoNz1fVd12byb3btqZY";

// Gemini API Key from your .env file
const GEMINI_API_KEY = "AIzaSyBFIp9JNH2LuzEs1PJdJu8Pk0-CkeaHdF8";

// Replace with your Google Sheet ID (found in the URL of your spreadsheet)
const GOOGLE_SHEET_ID = "1hq3hm-vmZrGDxe6QzaVqAkcBPHTh3FZS-4HI_xqPKCc";

function processTrustpilotReviews() {
  // Search for unread Trustpilot review emails
  const threads = GmailApp.search('is:unread from:noreply.notifications@trustpilot.com subject:"You\'ve got a new"');

  if (threads.length === 0) {
    Logger.log("No new reviews found.");
    return;
  }

  for (let i = 0; i < threads.length; i++) {
    const thread = threads[i];
    const messages = thread.getMessages();

    for (let j = 0; j < messages.length; j++) {
      const msg = messages[j];

      // We only want to process messages we haven't seen before
      if (msg.isUnread()) {
        const subject = msg.getSubject();
        const body = msg.getPlainBody();

        // Extract rating from subject
        let rating = "Unknown";
        const ratingMatch = subject.match(/(\d+)-star/);
        if (ratingMatch) {
          rating = ratingMatch[1];
        }

        // Extract name
        let name = "A customer";
        const nameMatch = body.match(/^\s*(.+?) just left a new \d+-star review/m);
        if (nameMatch) {
          name = nameMatch[1].trim();
        }

        // Extract comment
        let comment = "Could not parse comment.";
        const startMarker = "review of superhairpieces.com:";
        const endMarker = "See this review and reply";
        const startIdx = body.indexOf(startMarker);
        const endIdx = body.indexOf(endMarker);

        if (startIdx !== -1 && endIdx !== -1) {
          comment = body.substring(startIdx + startMarker.length, endIdx).trim();
        }

        // Get improvement suggestion from Gemini
        let suggestion = "No actionable comment provided.";
        if (comment && comment.length > 5 && comment !== "Could not parse comment.") {
          suggestion = getGeminiSuggestion(comment, rating);
          Utilities.sleep(2000); // Throttle to prevent 429 Too Many Requests errors
        }

        // Send to Google Chat webhook
        sendToGoogleChat(name, rating, comment, suggestion);

        // Write to Google Sheet
        writeToGoogleSheet(name, rating, comment, suggestion);

        // Mark message as read so it isn't processed again next time
        msg.markRead();
        Logger.log(`Processed ${rating}-star review from ${name}.`);
      }
    }
  }
}

function writeToGoogleSheet(name, rating, comment, suggestion) {
  try {
    const sheet = SpreadsheetApp.openById(GOOGLE_SHEET_ID).getActiveSheet();
    const timestamp = new Date();

    // Append a new row at the bottom of the sheet
    sheet.appendRow([timestamp, name, rating, comment, suggestion]);

    Logger.log("Successfully written to Google Sheet.");
  } catch (e) {
    Logger.log("Error writing to Google Sheet: " + e);
  }
}

function getGeminiSuggestion(comment, rating) {
  const url = `https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-pro:generateContent?key=${GEMINI_API_KEY}`;

  const prompt = `A customer left a ${rating}-star review for our hairpiece company (Superhairpieces) with the following comment:\n"${comment}"\n\nPlease provide a brief, 1-2 sentence actionable improvement suggestion for our team based on this feedback. Be concise, professional, and directly address the customer's concern. If the review is completely positive and no improvement is needed, suggest a quick way to capitalize on it or provide a short encouraging message for the team.`;

  const payload = {
    "contents": [{
      "parts": [{ "text": prompt }]
    }],
    "generationConfig": {
      "temperature": 0.4
    }
  };

  const options = {
    "method": "post",
    "contentType": "application/json",
    "payload": JSON.stringify(payload),
    "muteHttpExceptions": true
  };

  try {
    const response = UrlFetchApp.fetch(url, options);
    const data = JSON.parse(response.getContentText());

    if (data.error) {
      Logger.log("API Error: " + JSON.stringify(data.error));
      return `AI temporarily unavailable (API Error ${data.error.code}).`;
    }

    if (data && data.candidates && data.candidates[0] && data.candidates[0].content && data.candidates[0].content.parts) {
      const parts = data.candidates[0].content.parts;
      for (let i = 0; i < parts.length; i++) {
        if (parts[i].text) {
          return parts[i].text.trim();
        }
      }
    }
    return "Could not generate a suggestion.";
  } catch (e) {
    Logger.log("Error calling Gemini API: " + e);
    return "Error getting suggestion from AI.";
  }
}

function sendToGoogleChat(name, rating, comment, suggestion) {
  const payload = {
    "text": `⭐ *New Trustpilot Review!* ⭐\n\n*Customer:* ${name}\n*Rating:* ${rating}-star\n\n*Comment:*\n"${comment}"\n\n💡 *Improvement Suggestion (AI):*\n${suggestion}`
  };

  const options = {
    "method": "post",
    "contentType": "application/json",
    "payload": JSON.stringify(payload)
  };

  try {
    UrlFetchApp.fetch(GCHAT_WEBHOOK_URL, options);
  } catch (e) {
    Logger.log("Error sending to Google Chat: " + e);
  }
}
