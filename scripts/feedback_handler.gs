/**
 * Google Apps Script - Feedback Handler for Cardiology Digest
 *
 * Deploy as Web App:
 * 1. Open Google Sheets (same sheet as subscribers)
 * 2. Extensions > Apps Script
 * 3. Paste this code
 * 4. Deploy > New deployment > Web app
 * 5. Execute as: Me, Who has access: Anyone
 * 6. Copy the Web App URL to FEEDBACK_WEBHOOK_URL env var
 */

function doGet(e) {
  try {
    var params = e.parameter;
    var action = params.action || 'feedback';
    var user = params.user || '';

    // Handle "view saves" action
    if (action === 'view') {
      if (!user) {
        return HtmlService.createHtmlOutput(errorPage('Missing user parameter'));
      }
      var saves = getUserSavesInternal(user);
      return HtmlService.createHtmlOutput(viewSavesPage(saves, user));
    }

    // Handle feedback action (default)
    var pmid = params.pmid || '';
    var title = params.title || '';
    var vote = params.vote || '';

    if (!user || !pmid || !vote) {
      return HtmlService.createHtmlOutput(errorPage('Missing required parameters'));
    }

    // Get or create feedback sheet
    var ss = SpreadsheetApp.getActiveSpreadsheet();
    var feedbackSheet;
    try {
      feedbackSheet = ss.getSheetByName('feedback');
    } catch (err) {
      feedbackSheet = null;
    }

    if (!feedbackSheet) {
      feedbackSheet = ss.insertSheet('feedback');
      feedbackSheet.appendRow(['timestamp', 'user', 'pmid', 'title', 'vote']);
      feedbackSheet.getRange(1, 1, 1, 5).setFontWeight('bold');
    }

    // Log the feedback
    feedbackSheet.appendRow([
      new Date().toISOString(),
      user,
      pmid,
      decodeURIComponent(title),
      vote
    ]);

    // Return appropriate response based on vote
    if (vote === 'yes') {
      return HtmlService.createHtmlOutput(savedPage(decodeURIComponent(title)));
    } else {
      return HtmlService.createHtmlOutput(notedPage());
    }

  } catch (err) {
    return HtmlService.createHtmlOutput(errorPage(err.toString()));
  }
}

function savedPage(title) {
  return `
<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Saved</title>
  <style>
    body {
      font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
      display: flex;
      justify-content: center;
      align-items: center;
      min-height: 100vh;
      margin: 0;
      background: #f5f5f5;
    }
    .card {
      background: white;
      padding: 40px;
      border-radius: 12px;
      box-shadow: 0 2px 8px rgba(0,0,0,0.1);
      text-align: center;
      max-width: 400px;
    }
    .check {
      width: 48px;
      height: 48px;
      background: #e8f5e9;
      border-radius: 50%;
      display: flex;
      align-items: center;
      justify-content: center;
      margin: 0 auto 16px;
      color: #2e7d32;
      font-size: 24px;
    }
    h1 { font-size: 20px; margin: 0 0 8px; color: #1a1a1a; }
    p { color: #666; margin: 0; font-size: 14px; line-height: 1.5; }
  </style>
</head>
<body>
  <div class="card">
    <div class="check">&#10003;</div>
    <h1>Saved</h1>
    <p>This article will appear in your "Your Saves" section next week.</p>
  </div>
  <script>setTimeout(function() { window.close(); }, 2000);</script>
</body>
</html>`;
}

function notedPage() {
  return `
<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Noted</title>
  <style>
    body {
      font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
      display: flex;
      justify-content: center;
      align-items: center;
      min-height: 100vh;
      margin: 0;
      background: #f5f5f5;
    }
    .card {
      background: white;
      padding: 40px;
      border-radius: 12px;
      box-shadow: 0 2px 8px rgba(0,0,0,0.1);
      text-align: center;
      max-width: 400px;
    }
    h1 { font-size: 20px; margin: 0 0 8px; color: #1a1a1a; }
    p { color: #666; margin: 0; font-size: 14px; }
  </style>
</head>
<body>
  <div class="card">
    <h1>Noted</h1>
    <p>Thanks for the feedback.</p>
  </div>
  <script>setTimeout(function() { window.close(); }, 2000);</script>
</body>
</html>`;
}

function errorPage(message) {
  return `
<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8">
  <title>Error</title>
  <style>
    body {
      font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
      display: flex;
      justify-content: center;
      align-items: center;
      min-height: 100vh;
      margin: 0;
      background: #f5f5f5;
    }
    .card {
      background: white;
      padding: 40px;
      border-radius: 12px;
      text-align: center;
    }
    h1 { color: #c62828; }
  </style>
</head>
<body>
  <div class="card">
    <h1>Something went wrong</h1>
    <p>${message}</p>
  </div>
</body>
</html>`;
}


function viewSavesPage(saves, userEmail) {
  var articlesList = '';

  if (saves.length === 0) {
    articlesList = `
      <div style="text-align:center; padding:40px; color:#888;">
        <p style="font-size:16px; margin:0;">No saved articles yet.</p>
        <p style="font-size:14px; margin-top:8px;">Click "Yes" on articles in your digest to save them here.</p>
      </div>
    `;
  } else {
    for (var i = 0; i < saves.length; i++) {
      var save = saves[i];
      var pubmedUrl = 'https://pubmed.ncbi.nlm.nih.gov/' + save.pmid + '/';
      var dateStr = '';
      try {
        var d = new Date(save.timestamp);
        dateStr = d.toLocaleDateString('en-US', { month: 'short', day: 'numeric', year: 'numeric' });
      } catch (e) {
        dateStr = '';
      }

      articlesList += `
        <div style="border-bottom:1px solid #f0f0f0; padding:16px 0;">
          <a href="${pubmedUrl}" target="_blank" style="color:#1a1a1a; text-decoration:none; font-size:15px; line-height:1.5; display:block;">
            ${save.title}
          </a>
          <div style="font-size:12px; color:#888; margin-top:6px;">
            Saved ${dateStr} · <a href="${pubmedUrl}" target="_blank" style="color:#666;">View on PubMed</a>
          </div>
        </div>
      `;
    }
  }

  return `
<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Your Saved Articles</title>
  <style>
    * { box-sizing: border-box; }
    body {
      font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
      margin: 0;
      padding: 0;
      background: #f5f5f5;
      color: #1a1a1a;
    }
    .container {
      max-width: 640px;
      margin: 0 auto;
      padding: 24px 16px;
    }
    .header {
      background: white;
      border: 1px solid #e0e0e0;
      border-radius: 8px;
      padding: 24px;
      margin-bottom: 20px;
    }
    .header h1 {
      font-size: 22px;
      font-weight: 600;
      margin: 0 0 6px 0;
    }
    .header p {
      font-size: 13px;
      color: #666;
      margin: 0;
    }
    .articles {
      background: white;
      border: 1px solid #e0e0e0;
      border-radius: 8px;
      padding: 8px 20px;
    }
    .footer {
      text-align: center;
      padding: 20px;
      font-size: 12px;
      color: #999;
    }
  </style>
</head>
<body>
  <div class="container">
    <div class="header">
      <h1>Your Saved Articles</h1>
      <p>${saves.length} article${saves.length !== 1 ? 's' : ''} saved</p>
    </div>

    <div class="articles">
      ${articlesList}
    </div>

    <div class="footer">
      Cardiology Weekly Digest
    </div>
  </div>
</body>
</html>`;
}


function getUserSavesInternal(userEmail) {
  var ss = SpreadsheetApp.getActiveSpreadsheet();
  var feedbackSheet;

  try {
    feedbackSheet = ss.getSheetByName('feedback');
  } catch (err) {
    return [];
  }

  if (!feedbackSheet) {
    return [];
  }

  var data = feedbackSheet.getDataRange().getValues();
  var saves = [];
  var seenPmids = {};  // Deduplicate by PMID

  // Skip header row
  for (var i = 1; i < data.length; i++) {
    var row = data[i];
    var timestamp = row[0];
    var user = row[1];
    var pmid = row[2];
    var title = row[3];
    var vote = row[4];

    if (user.toString().toLowerCase() === userEmail.toLowerCase() && vote === 'yes') {
      if (!seenPmids[pmid]) {
        saves.push({
          pmid: pmid,
          title: title,
          timestamp: timestamp
        });
        seenPmids[pmid] = true;
      }
    }
  }

  // Return most recent first
  saves.reverse();
  return saves;
}


/**
 * Alias for external calls (backwards compatibility)
 */
function getUserSaves(userEmail) {
  return getUserSavesInternal(userEmail);
}


// ============================================
// WELCOME EMAIL - Triggered on form submit
// ============================================

/**
 * Normalize specialty name from form response
 */
function normalizeSpecialty(specialty) {
  var lower = (specialty || '').toLowerCase().trim();
  if (lower === 'cardiology') return 'cardiology';
  if (lower === 'gp' || lower === 'general practice') return 'gp';
  if (lower === 'spine' || lower === 'spine surgery') return 'spine';
  return 'cardiology'; // Default fallback
}

/**
 * Trigger this function when the subscribe form is submitted.
 *
 * Setup:
 * 1. In Apps Script, go to Triggers (clock icon)
 * 2. Add Trigger:
 *    - Function: onFormSubmit
 *    - Event source: From spreadsheet
 *    - Event type: On form submit
 * 3. Save and authorize
 */
function onFormSubmit(e) {
  try {
    // Only trigger for subscribers sheet, not unsubscribers or others
    var sheetName = e.range.getSheet().getName().toLowerCase();
    if (sheetName !== 'subscribers') return;

    // Get form response data
    var values = e.values;
    if (!values || values.length < 3) return;

    // Form structure: Timestamp, Firstname, Email, Specialty (columns A, B, C, D)
    var firstname = values[1] || '';
    var email = values[2] || '';
    var specialty = normalizeSpecialty(values[3] || 'cardiology');

    if (!email || !email.includes('@')) return;

    sendWelcomeEmail(email, firstname, specialty);

  } catch (err) {
    console.error('Welcome email error: ' + err.toString());
  }
}

/**
 * Send welcome email to new subscriber
 */
function sendWelcomeEmail(email, firstname, specialty) {
  specialty = specialty || 'cardiology';
  var greeting = firstname ? ('Hi ' + firstname + ',') : 'Hi,';

  // Specialty-specific configuration
  var config = {
    cardiology: {
      title: 'Cardiology Weekly',
      displayName: 'cardiology',
      senderName: 'Ike Chukwudi | Cardiology Digest',
      enableFeedback: true
    },
    gp: {
      title: 'General Practice Weekly',
      displayName: 'general practice',
      senderName: 'Ike Chukwudi | General Practice Digest',
      enableFeedback: false
    },
    spine: {
      title: 'Spine Surgery Weekly',
      displayName: 'spine surgery',
      senderName: 'Ike Chukwudi | Spine Surgery Digest',
      enableFeedback: false
    }
  };

  var cfg = config[specialty] || config.cardiology;
  var subject = 'Welcome to ' + cfg.title;

  // Build feedback instructions (only for cardiology)
  var feedbackSection = '';
  if (cfg.enableFeedback) {
    feedbackSection = `
      <div style="margin-bottom:16px;">
        <h3 style="font-size:14px; margin:0 0 6px; color:#1a1a1a;">Giving feedback</h3>
        <p style="font-size:14px; color:#555; line-height:1.5; margin:0;">
          Under each article you'll see: <em>Was this useful? Yes · No</em><br>
          Click <strong>Yes</strong> to save articles you find valuable. A new tab will open briefly to confirm — just close it and continue reading.
        </p>
      </div>

      <div style="margin-bottom:16px;">
        <h3 style="font-size:14px; margin:0 0 6px; color:#1a1a1a;">Viewing your saved articles</h3>
        <p style="font-size:14px; color:#555; line-height:1.5; margin:0;">
          At the bottom of every email, click <strong>View your saved articles</strong> to see everything you've marked as useful. It's your personal reading list.
        </p>
      </div>

      <div style="margin-bottom:16px;">
        <h3 style="font-size:14px; margin:0 0 6px; color:#1a1a1a;">Your Saves section</h3>
        <p style="font-size:14px; color:#555; line-height:1.5; margin:0;">
          If you've saved articles before, you'll see a <strong>Your Saves →</strong> section at the top of your next digest. Click it to view all your saves.
        </p>
      </div>
    `;
  }

  var htmlBody = `
<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
</head>
<body style="margin:0; padding:0; background:#f5f5f5; font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;">
  <div style="max-width:600px; margin:0 auto; padding:24px 16px;">

    <div style="background:#fff; border:1px solid #e0e0e0; border-radius:8px; padding:24px; margin-bottom:20px;">
      <h1 style="font-size:22px; margin:0 0 16px; color:#1a1a1a;">Welcome to ${cfg.title}</h1>
      <p style="font-size:15px; color:#333; line-height:1.6; margin:0 0 16px;">
        ${greeting}
      </p>
      <p style="font-size:15px; color:#333; line-height:1.6; margin:0 0 16px;">
        You're receiving this email because you signed up to receive the ${cfg.displayName} digest. Every Sunday, you'll get a curated summary of the latest ${cfg.displayName} research from top journals.
      </p>
    </div>

    <div style="background:#fff; border:1px solid #e0e0e0; border-radius:8px; padding:24px; margin-bottom:20px;">
      <h2 style="font-size:16px; margin:0 0 16px; color:#1a1a1a;">How to get the most out of your digest</h2>

      <div style="margin-bottom:16px;">
        <h3 style="font-size:14px; margin:0 0 6px; color:#1a1a1a;">Clicking article titles</h3>
        <p style="font-size:14px; color:#555; line-height:1.5; margin:0;">
          Each article title is a link. Click it to go straight to the PubMed abstract.
        </p>
      </div>

      ${feedbackSection}

      <div>
        <h3 style="font-size:14px; margin:0 0 6px; color:#1a1a1a;">Avoid the spam folder</h3>
        <p style="font-size:14px; color:#555; line-height:1.5; margin:0;">
          Add the sender to your contacts. If it lands in spam, mark it as "Not spam". On Gmail, drag it to your Primary tab if it appears in Promotions.
        </p>
      </div>
    </div>

    <div style="text-align:center; color:#999; font-size:12px; padding:16px;">
      Your first digest will arrive this Sunday.<br>
      To help your email provider recognize this as legitimate mail, please reply with "thanks" to confirm.<br>
      Questions? Just reply to this email.
    </div>

  </div>
</body>
</html>`;

  GmailApp.sendEmail(email, subject, 'Welcome to ' + cfg.title, {
    htmlBody: htmlBody,
    name: cfg.senderName
  });
}

/**
 * Test functions - send welcome email to yourself
 * Run these manually to test the welcome emails
 */
function testWelcomeEmailCardiology() {
  var testEmail = Session.getActiveUser().getEmail();
  sendWelcomeEmail(testEmail, 'Test', 'cardiology');
}

function testWelcomeEmailGP() {
  var testEmail = Session.getActiveUser().getEmail();
  sendWelcomeEmail(testEmail, 'Test', 'gp');
}

function testWelcomeEmailSpine() {
  var testEmail = Session.getActiveUser().getEmail();
  sendWelcomeEmail(testEmail, 'Test', 'spine');
}
