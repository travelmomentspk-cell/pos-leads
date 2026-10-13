/**
 * Free receiver for the lead pipeline. Paste into Extensions > Apps Script of the
 * "Canada POS Leads" sheet, change TOKEN, then Deploy > New deployment > Web app
 * (Execute as: Me, Who has access: Anyone). Copy the web app URL into config.yaml.
 * Only requests that carry the right token can add rows.
 */
const TOKEN = 'CHANGE_THIS_TO_A_LONG_RANDOM_SECRET';
const ALLOWED_TABS = ['Raw_Staging', 'Qualified_Leads', 'Review_Queue', 'Rejected_Log', 'Run_Log'];

function doPost(e) {
  try {
    const p = JSON.parse(e.postData.contents);
    if (p.token !== TOKEN) return out_({ ok: false, error: 'bad token' });
    if (ALLOWED_TABS.indexOf(p.tab) === -1) return out_({ ok: false, error: 'unknown tab' });
    const rows = p.rows || [];
    if (!rows.length) return out_({ ok: true, added: 0 });
    const sheet = SpreadsheetApp.getActiveSpreadsheet().getSheetByName(p.tab);
    if (!sheet) return out_({ ok: false, error: 'tab not found' });
    const width = rows[0].length;
    const start = lastDataRow_(sheet) + 1;
    const need = start + rows.length - 1 - sheet.getMaxRows();
    if (need > 0) sheet.insertRowsAfter(sheet.getMaxRows(), need);
    if (sheet.getMaxColumns() < width) sheet.insertColumnsAfter(sheet.getMaxColumns(), width - sheet.getMaxColumns());
    sheet.getRange(start, 1, rows.length, width).setValues(rows);
    return out_({ ok: true, added: rows.length, firstRow: start });
  } catch (err) {
    return out_({ ok: false, error: String(err) });
  }
}

function lastDataRow_(sheet) {
  const vals = sheet.getRange(1, 1, sheet.getMaxRows(), 1).getValues();
  for (let i = vals.length - 1; i >= 0; i--) if (vals[i][0] !== '') return i + 1;
  return 1;
}

function out_(obj) {
  return ContentService.createTextOutput(JSON.stringify(obj)).setMimeType(ContentService.MimeType.JSON);
}
