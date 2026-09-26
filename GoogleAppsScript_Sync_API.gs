/**
 * ==============================================================================
 * TechFEST'26 SLIET — Real-Time Google Sheet Sync & Audit Engine (Backend API)
 * SPREADSHEET ID: 1gPlVQ10QzJsnss_HAxRcE9zLoyjgQO4GcksgqZ_bxzg
 * ==============================================================================
 * 
 * DEPLOYMENT INSTRUCTIONS (Takes 60 Seconds):
 * 1. Open the Google Sheet:
 *    https://docs.google.com/spreadsheets/d/1gPlVQ10QzJsnss_HAxRcE9zLoyjgQO4GcksgqZ_bxzg/edit
 * 2. In the top menu, go to: Extensions > Apps Script.
 * 3. Delete any code in the editor, and paste this ENTIRE file content.
 * 4. Click Save (Ctrl+S or Cmd+S).
 * 5. Click the blue 'Deploy' button (top right) > 'New deployment'.
 * 6. Click the gear icon (⚙️) next to 'Select type' and choose 'Web app'.
 * 7. Set:
 *    - Description: TechFEST Calling Live Sync API
 *    - Execute as: Me (techfest@sliet.ac.in)
 *    - Who has access: Anyone
 * 8. Click 'Deploy', authorize Google permissions if prompted, and copy the Web App URL!
 * 9. Paste this Web App URL into the Calling App's Admin Panel under "Google Sheet Sync Settings".
 * 
 * That's it! Every call outcome, payment, attendance tag, remarks, and login/logout log
 * will now instantly write to this Google Sheet and the 'Audit Logs' tab in real-time!
 */

const CALLER_TABS = [
  'Balwant', 'Shreya Mahanti', 'Ritik Kumar', 'Priyanshi Deshwal',
  'Ayansh Sahay', 'Aahna', 'Supriyaaa', 'palak'
];

/**
 * Handle incoming POST requests from the GitHub Pages Web App
 */
function doPost(e) {
  try {
    let payload = {};
    if (e && e.postData && e.postData.contents) {
      try {
        payload = JSON.parse(e.postData.contents);
      } catch (parseErr) {
        // Fallback for form-encoded or raw strings
        payload = e.parameter || {};
      }
    } else if (e && e.parameter) {
      payload = e.parameter;
    }

    const ss = SpreadsheetApp.getActiveSpreadsheet();
    const action = payload.action || 'updateLead';

    if (action === 'updateLead') {
      const result = handleLeadUpdate(ss, payload);
      return sendJsonResponse({ success: true, message: 'Lead updated successfully', details: result });
    } else if (action === 'addBatchLeads' || action === 'addLeads') {
      const result = handleAddBatchLeads(ss, payload);
      return sendJsonResponse({ success: true, message: 'Batch leads added successfully', details: result });
    } else if (action === 'logAuth') {
      handleAuthLog(ss, payload);
      return sendJsonResponse({ success: true, message: 'Authentication audit event logged successfully' });
    } else if (action === 'batchSync') {
      const result = handleBatchSync(ss, payload);
      return sendJsonResponse({ success: true, message: 'Batch synchronization complete', synced: result });
    } else {
      return sendJsonResponse({ success: false, error: 'Unknown action specified: ' + action });
    }
  } catch (err) {
    return sendJsonResponse({ success: false, error: err.toString() });
  }
}

/**
 * Handle incoming GET requests (Health check / Ping / Data pull)
 */
function doGet(e) {
  try {
    const ss = SpreadsheetApp.getActiveSpreadsheet();
    const action = (e && e.parameter && e.parameter.action) || 'ping';

    if (action === 'getData') {
      const data = fetchAllCallingData(ss);
      return sendJsonResponse({ success: true, data: data, timestamp: new Date().toISOString() });
    }

    return sendJsonResponse({
      status: 'ok',
      service: "TechFEST'26 Real-Time Sheet Sync API",
      spreadsheet: ss.getName(),
      spreadsheetId: ss.getId(),
      time: new Date().toISOString()
    });
  } catch (err) {
    return sendJsonResponse({ success: false, error: err.toString() });
  }
}

/**
 * Update candidate details in caller's sheet and record in Audit Logs
 */
function handleLeadUpdate(ss, data) {
  const callerName = data.callerName;
  if (!callerName) throw new Error('Missing callerName in payload');

  const sheet = ss.getSheetByName(callerName);
  if (!sheet) throw new Error('Caller sheet not found for: ' + callerName);

  const values = sheet.getDataRange().getValues();
  let targetRow = -1;

  const searchName = String(data.leadName || '').trim().toLowerCase();
  const searchPhone = String(data.leadPhone || data.mobile || '').replace(/\D/g, '');

  // Search by exact name match first, or clean phone match
  for (let r = 1; r < values.length; r++) {
    const rowName = String(values[r][0] || '').trim().toLowerCase();
    const rowPhone = String(values[r][2] || '').replace(/\D/g, '');

    if (searchName && rowName === searchName) {
      targetRow = r + 1;
      break;
    }
    if (searchPhone && rowPhone && (rowPhone.endsWith(searchPhone) || searchPhone.endsWith(rowPhone))) {
      targetRow = r + 1;
      break;
    }
  }

  // Fallback to row index if specified and within range
  if (targetRow === -1 && data.leadId) {
    const idx = parseInt(data.leadId) + 1;
    if (idx <= values.length) targetRow = idx;
  }

  // Update caller sheet row if found
  if (targetRow > 0) {
    // Col G (7): Call Status
    if (data.callStatus !== undefined) sheet.getRange(targetRow, 7).setValue(data.callStatus);
    // Col H (8): Payment Status
    if (data.payStatus !== undefined) sheet.getRange(targetRow, 8).setValue(data.payStatus);
    // Col I (9): Payment Amount
    if (data.amount !== undefined) {
      const num = parseFloat(data.amount);
      sheet.getRange(targetRow, 9).setValue(isNaN(num) || !data.amount ? '' : num);
    }
    // Col J (10): Class Representative (CR) Number
    if (data.crNumber !== undefined) sheet.getRange(targetRow, 10).setValue(String(data.crNumber));
    // Col K (11): Expected Payment Date
    if (data.expectedDate !== undefined) sheet.getRange(targetRow, 11).setValue(data.expectedDate);
    // Col L (12): Caller Remarks
    if (data.remarks !== undefined) sheet.getRange(targetRow, 12).setValue(data.remarks);
    // Col M (13): Attended?
    if (data.attended !== undefined) {
      const attText = (data.attended === true || data.attended === 'true' || data.attended === 'Attended') ? 'Attended' : 'Not Attended';
      sheet.getRange(targetRow, 13).setValue(attText);
    }
  }

  // Record audit entry in Audit Logs tab
  const auditSheet = getOrCreateAuditSheet(ss);
  const now = new Date();
  const timestamp = data.timestamp || Utilities.formatDate(now, 'Asia/Kolkata', 'yyyy-MM-dd hh:mm:ss a') + ' IST';
  
  const payStr = (data.payStatus || 'Pending') + (data.amount ? ' (₹' + data.amount + ')' : '');
  const attStr = (data.attended === true || data.attended === 'true' || data.attended === 'Attended') ? 'Attended' : 'Not Attended';

  auditSheet.appendRow([
    timestamp,
    'LEAD_UPDATE',
    data.updatedBy || callerName,
    'Caller',
    data.leadName || values[targetRow - 1]?.[0] || '-',
    data.leadPhone || data.mobile || values[targetRow - 1]?.[2] || '-',
    data.callStatus || 'Pending',
    payStr,
    attStr,
    data.remarks || 'Updated via Calling Web App'
  ]);

  return { rowFound: targetRow > 0, rowIndex: targetRow };
}

/**
 * Handle user and admin login/logout and password reset audit logs
 */
function handleAuthLog(ss, data) {
  const auditSheet = getOrCreateAuditSheet(ss);
  const now = new Date();
  const timestamp = data.timestamp || Utilities.formatDate(now, 'Asia/Kolkata', 'yyyy-MM-dd hh:mm:ss a') + ' IST';

  auditSheet.appendRow([
    timestamp,
    data.event || data.eventType || 'AUTH_EVENT',
    data.email || data.userEmail || data.name || 'User',
    data.role || 'user',
    '-',
    '-',
    '-',
    '-',
    '-',
    (data.status ? '[' + data.status + '] ' : '') + (data.details || '') + (data.device ? ' (' + data.device + ')' : '')
  ]);
}

/**
 * Handle batch syncing of leads and logs
 */
function handleBatchSync(ss, payload) {
  let leadsCount = 0;
  let logsCount = 0;

  if (Array.isArray(payload.leads)) {
    payload.leads.forEach(leadUpdate => {
      try {
        handleLeadUpdate(ss, leadUpdate);
        leadsCount++;
      } catch (e) {}
    });
  }

  if (Array.isArray(payload.logs)) {
    payload.logs.forEach(logEntry => {
      try {
        handleAuthLog(ss, logEntry);
        logsCount++;
      } catch (e) {}
    });
  }

  return { leadsUpdated: leadsCount, logsRecorded: logsCount };
}

/**
 * Append a newly scraped batch of leads across caller sheets
 */
function handleAddBatchLeads(ss, payload) {
  const newLeads = payload.leads || [];
  if (!Array.isArray(newLeads) || newLeads.length === 0) {
    return { added: 0 };
  }

  let addedCount = 0;
  newLeads.forEach(item => {
    const callerName = item.callerName;
    if (!callerName) return;
    const sheet = ss.getSheetByName(callerName);
    if (!sheet) return;

    const nextRow = sheet.getLastRow() + 1;
    const callFormula = '=HYPERLINK("tel:"&C' + nextRow + ', "📞 Call")';
    
    sheet.appendRow([
      item.name || '',
      item.college || '',
      item.mobile || '',
      callFormula,
      item.email || '',
      item.events || '',
      item.callStatus || 'Pending',
      item.payStatus || 'Pending',
      item.amount || '',
      item.crNumber || '',
      item.expectedDate || '',
      item.remarks || '',
      item.attended ? 'Attended' : 'Not Attended'
    ]);
    addedCount++;
  });

  const auditSheet = getOrCreateAuditSheet(ss);
  const now = new Date();
  const timestamp = Utilities.formatDate(now, 'Asia/Kolkata', 'yyyy-MM-dd hh:mm:ss a') + ' IST';
  auditSheet.appendRow([
    timestamp,
    'BATCH_LEADS_ADDED',
    payload.source || 'GitHub Actions Nightly Scraper',
    'System',
    '-',
    '-',
    '-',
    '-',
    '-',
    'Appended ' + addedCount + ' newly scraped leads across caller tabs.'
  ]);

  return { added: addedCount };
}

/**
 * Read all calling data from all 8 caller sheets
 */
function fetchAllCallingData(ss) {
  const result = {};

  CALLER_TABS.forEach(callerName => {
    const sheet = ss.getSheetByName(callerName);
    if (!sheet) {
      result[callerName] = [];
      return;
    }

    const data = sheet.getDataRange().getValues();
    const leads = [];

    for (let r = 1; r < data.length; r++) {
      const row = data[r];
      if (!row[0]) continue;

      let expDateStr = '';
      if (row[10] instanceof Date) {
        expDateStr = Utilities.formatDate(row[10], 'Asia/Kolkata', 'yyyy-MM-dd');
      } else if (row[10]) {
        expDateStr = String(row[10]);
      }

      const attendedVal = String(row[12] || '').toLowerCase();
      const isAttended = attendedVal === 'attended' || attendedVal === 'yes' || attendedVal === 'true';

      leads.push({
        id: r,
        name: String(row[0] || ''),
        college: String(row[1] || ''),
        mobile: String(row[2] || ''),
        email: String(row[4] || ''),
        events: String(row[5] || ''),
        callStatus: String(row[6] || 'Pending'),
        payStatus: String(row[7] || 'Pending'),
        amount: row[8] ? String(row[8]) : '',
        crNumber: String(row[9] || ''),
        expectedDate: expDateStr,
        remarks: String(row[11] || ''),
        attended: isAttended
      });
    }
    result[callerName] = leads;
  });

  return result;
}

/**
 * Helper to get or create the Audit Logs sheet with correct headers and styling
 */
function getOrCreateAuditSheet(ss) {
  let auditSheet = ss.getSheetByName('Audit Logs');
  if (!auditSheet) {
    auditSheet = ss.insertSheet('Audit Logs');
    auditSheet.appendRow([
      'Timestamp (IST)',
      'Event Type',
      'User / Caller',
      'User Role',
      'Candidate Name',
      'Candidate Mobile',
      'Call Status',
      'Payment Status & Amount',
      'Attended?',
      'Notes / Event Details'
    ]);
    const headerRange = auditSheet.getRange(1, 1, 1, 10);
    headerRange.setBackground('#131b2e');
    headerRange.setFontColor('#ffffff');
    headerRange.setFontWeight('bold');
    auditSheet.setFrozenRows(1);
  }
  return auditSheet;
}

/**
 * Return JSON response formatted for CORS
 */
function sendJsonResponse(obj) {
  return ContentService.createTextOutput(JSON.stringify(obj))
    .setMimeType(ContentService.MimeType.JSON);
}
