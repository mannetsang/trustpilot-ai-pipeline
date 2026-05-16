// ─── Configuration (stored in Apps Script Properties — not hardcoded) ────────
// Set these once via: Extensions → Apps Script → Project Settings → Script Properties
// Keys: GCHAT_WEBHOOK_URL, GEMINI_API_KEY, GOOGLE_SHEET_ID, TRUSTPILOT_SECRET
function getConfig() {
  const props = PropertiesService.getScriptProperties();
  return {
    gchatUrl:        props.getProperty('GCHAT_WEBHOOK_URL'),
    geminiKey:       props.getProperty('GEMINI_API_KEY'),
    sheetId:         props.getProperty('GOOGLE_SHEET_ID'),
    trustpilotSecret: props.getProperty('TRUSTPILOT_SECRET'),
  };
}

// ─── Trustpilot Webhook Receiver ──────────────────────────────────────────────
// Deploy this script as a Web App (Execute as: Me, Who has access: Anyone)
// and paste the /exec URL into register_webhook.py
function doPost(e) {
  try {
    const raw = e.postData.contents;
    const payload = JSON.parse(raw);
    const cfg = getConfig();

    // Verify HMAC-SHA256 signature from Trustpilot
    const signature = e.parameter['X-Trustpilot-Signature'] ||
                      (e.headers && e.headers['X-Trustpilot-Signature']) || '';
    if (cfg.trustpilotSecret && signature) {
      const expected = computeHmac(cfg.trustpilotSecret, raw);
      if (expected !== signature) {
        Logger.log('Signature mismatch — ignoring request.');
        return jsonResponse({ status: 'unauthorized' }, 401);
      }
    }

    // Only handle new reviews
    if (payload.eventType !== 'review.created') {
      return jsonResponse({ status: 'ignored', reason: 'not a review.created event' });
    }

    const name    = (payload.consumer && payload.consumer.displayName) || 'A customer';
    const rating  = String(payload.stars || 'Unknown');
    const title   = payload.title  || '';
    const text    = payload.text   || '';
    const comment = title ? `${title}\n\n${text}` : text;

    let suggestion = 'No actionable comment provided.';
    if (comment.trim().length > 5) {
      suggestion = getGeminiSuggestion(comment, rating, cfg.geminiKey);
    }

    sendToGoogleChat(name, rating, comment, suggestion, cfg.gchatUrl);
    writeToGoogleSheet(name, rating, comment, suggestion, cfg.sheetId);

    Logger.log(`Webhook processed: ${rating}-star review from ${name}.`);
    return jsonResponse({ status: 'ok' });

  } catch (err) {
    Logger.log('doPost error: ' + err);
    return jsonResponse({ status: 'error', message: err.toString() }, 500);
  }
}

function computeHmac(secret, message) {
  const key  = Utilities.newBlob(secret).getBytes();
  const msg  = Utilities.newBlob(message).getBytes();
  const hash = Utilities.computeHmacSha256Signature(msg, key);
  return hash.map(b => ('0' + (b & 0xff).toString(16)).slice(-2)).join('');
}

function jsonResponse(obj, code) {
  return ContentService
    .createTextOutput(JSON.stringify(obj))
    .setMimeType(ContentService.MimeType.JSON);
}

// ─── Gmail Polling (fallback — runs on a time-based trigger) ─────────────────
function processTrustpilotReviews() {
  const cfg = getConfig();
  const threads = GmailApp.search('is:unread from:noreply.notifications@trustpilot.com subject:"You\'ve got a new"');

  if (threads.length === 0) {
    Logger.log('No new reviews found.');
    return;
  }

  for (const thread of threads) {
    for (const msg of thread.getMessages()) {
      if (!msg.isUnread()) continue;

      const subject = msg.getSubject();
      const body    = msg.getPlainBody();

      let rating = 'Unknown';
      const ratingMatch = subject.match(/(\d+)-star/);
      if (ratingMatch) rating = ratingMatch[1];

      let name = 'A customer';
      const nameMatch = body.match(/^\s*(.+?) just left a new \d+-star review/m);
      if (nameMatch) name = nameMatch[1].trim();

      let comment = 'Could not parse comment.';
      const startMarker = 'review of superhairpieces.com:';
      const endMarker   = 'See this review and reply';
      const startIdx = body.indexOf(startMarker);
      const endIdx   = body.indexOf(endMarker);
      if (startIdx !== -1 && endIdx !== -1) {
        comment = body.substring(startIdx + startMarker.length, endIdx).trim();
      }

      let suggestion = 'No actionable comment provided.';
      if (comment.length > 5 && comment !== 'Could not parse comment.') {
        suggestion = getGeminiSuggestion(comment, rating, cfg.geminiKey);
        Utilities.sleep(2000);
      }

      sendToGoogleChat(name, rating, comment, suggestion, cfg.gchatUrl);
      writeToGoogleSheet(name, rating, comment, suggestion, cfg.sheetId);
      msg.markRead();
      Logger.log(`Processed ${rating}-star review from ${name}.`);
    }
  }
}

// ─── Shared Helpers ───────────────────────────────────────────────────────────
function getGeminiSuggestion(comment, rating, apiKey) {
  const url = `https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-pro:generateContent?key=${apiKey}`;
  const prompt =
    `A customer left a ${rating}-star review for our hairpiece company (Superhairpieces) ` +
    `with the following comment:\n"${comment}"\n\n` +
    `Provide a brief, 1-2 sentence actionable improvement suggestion for our team. ` +
    `Be concise and professional. If the review is fully positive with no complaint, ` +
    `suggest a quick way to capitalise on it or encourage the team.`;

  const options = {
    method: 'post',
    contentType: 'application/json',
    payload: JSON.stringify({
      contents: [{ parts: [{ text: prompt }] }],
      generationConfig: { temperature: 0.4 },
    }),
    muteHttpExceptions: true,
  };

  try {
    const data = JSON.parse(UrlFetchApp.fetch(url, options).getContentText());
    if (data.error) {
      Logger.log('Gemini API error: ' + JSON.stringify(data.error));
      return `AI temporarily unavailable (error ${data.error.code}).`;
    }
    const parts = data?.candidates?.[0]?.content?.parts || [];
    for (const part of parts) {
      if (part.text) return part.text.trim();
    }
    return 'Could not generate a suggestion.';
  } catch (err) {
    Logger.log('Gemini call failed: ' + err);
    return 'Error getting suggestion from AI.';
  }
}

function sendToGoogleChat(name, rating, comment, suggestion, webhookUrl) {
  const stars = '⭐'.repeat(Math.min(Number(rating) || 0, 5));
  const text =
    `${stars} *New Trustpilot Review* ${stars}\n\n` +
    `*Customer:* ${name}\n` +
    `*Rating:* ${rating}-star\n\n` +
    `*Comment:*\n"${comment}"\n\n` +
    `💡 *AI Suggestion:*\n${suggestion}`;

  try {
    UrlFetchApp.fetch(webhookUrl, {
      method: 'post',
      contentType: 'application/json',
      payload: JSON.stringify({ text }),
    });
  } catch (err) {
    Logger.log('Google Chat send failed: ' + err);
  }
}

function writeToGoogleSheet(name, rating, comment, suggestion, sheetId) {
  try {
    SpreadsheetApp.openById(sheetId).getActiveSheet()
      .appendRow([new Date(), name, rating, comment, suggestion]);
  } catch (err) {
    Logger.log('Sheet write failed: ' + err);
  }
}
