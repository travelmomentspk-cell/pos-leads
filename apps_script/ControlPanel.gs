/**
 * LEAD SCRAPER CONTROL PANEL (Google Apps Script, bound to the Sheet)
 * Paste this whole file into Extensions > Apps Script, press Save, reload the Sheet,
 * then use the new menu: Leads > Setup (once). After that the Run now box on the Control tab starts a run.
 * The GitHub token is kept in Script Properties (private to this script), never in a cell.
 */
const CONTROL_TAB = 'Control';
const WORKFLOW_FILE = 'leads.yml';
const STALE_HOURS = 7;  // a run longer than this is treated as dead, so you are never locked out

function onOpen() {
  SpreadsheetApp.getUi().createMenu('Leads')
    .addItem('Run scraper', 'runScraper')
    .addItem('Show status', 'showStatus')
    .addItem('Setup (once)', 'setup')
    .addToUi();
}

function setup() {
  const ui = SpreadsheetApp.getUi();
  const repo = ui.prompt('Setup 1 of 2', 'Your GitHub repository as owner/name, for example mylogin/pos-leads', ui.ButtonSet.OK_CANCEL);
  if (repo.getSelectedButton() !== ui.Button.OK) return;
  const token = ui.prompt('Setup 2 of 2', 'Paste your GitHub fine-grained token. It is stored only inside this script.', ui.ButtonSet.OK_CANCEL);
  if (token.getSelectedButton() !== ui.Button.OK) return;
  PropertiesService.getScriptProperties().setProperties({
    GITHUB_REPO: repo.getResponseText().trim(),
    GITHUB_TOKEN: token.getResponseText().trim(),
    GITHUB_REF: 'main'
  });
  ScriptApp.getProjectTriggers()
    .filter(function (t) { return t.getHandlerFunction() === 'onControlEdit'; })
    .forEach(function (t) { ScriptApp.deleteTrigger(t); });
  ScriptApp.newTrigger('onControlEdit').forSpreadsheet(SpreadsheetApp.getActive()).onEdit().create();
  ui.alert('Setup done', 'Tick the Run now box on the Control tab, or use Leads > Run scraper.', ui.ButtonSet.OK);
}

// Installable edit trigger: ticking B6 starts a run and clears the box again.
function onControlEdit(e) {
  if (!e || !e.range) return;
  const r = e.range;
  if (r.getSheet().getName() !== CONTROL_TAB || r.getA1Notation() !== 'B6') return;
  if (e.value === 'TRUE' || r.getValue() === true) {
    r.setValue(false);
    startRun_(false);
  }
}

function runScraper() { startRun_(true); }

function showStatus() {
  const sh = SpreadsheetApp.getActive().getSheetByName(CONTROL_TAB);
  const v = sh.getRange('B8:B11').getValues();
  SpreadsheetApp.getUi().alert('Status: ' + v[0][0] + '\n' + v[1][0] + '\nUpdated: ' + v[2][0] + '\nRun ID: ' + v[3][0]);
}

function parseStamp_(s) {
  const m = /^(\d{4}-\d{2}-\d{2}) (\d{2}:\d{2}) UTC$/.exec(String(s || ''));
  return m ? new Date(m[1] + 'T' + m[2] + ':00Z') : null;
}

function setStatus_(sh, status, detail, runId) {
  sh.getRange('B8:B11').setValues([[status], [detail || ''], [Utilities.formatDate(new Date(), 'UTC', "yyyy-MM-dd HH:mm 'UTC'")], [runId || '']]);
}

function startRun_(interactive) {
  const sh = SpreadsheetApp.getActive().getSheetByName(CONTROL_TAB);
  const p = PropertiesService.getScriptProperties().getProperties();
  if (!p.GITHUB_TOKEN || !p.GITHUB_REPO) {
    setStatus_(sh, 'Failed: setup not done', 'Use the menu Leads > Setup (once)');
    return;
  }
  const status = String(sh.getRange('B8').getValue());
  const stamp = parseStamp_(sh.getRange('B10').getValue());
  const fresh = stamp && (new Date() - stamp) < STALE_HOURS * 3600 * 1000;
  if ((status === 'Queued' || status === 'Running') && fresh) {
    setStatus_(sh, status, 'A run is already in progress. Wait for Done or Failed.', String(sh.getRange('B11').getValue()));
    return;
  }
  const category = String(sh.getRange('B3').getValue() || 'ALL');
  const city = String(sh.getRange('B4').getValue() || 'ALL');
  const minScore = String(sh.getRange('B5').getValue() || '70');
  const runId = 'sheet-' + Utilities.formatDate(new Date(), 'UTC', 'yyyyMMdd-HHmmss');
  const url = 'https://api.github.com/repos/' + p.GITHUB_REPO + '/actions/workflows/' + WORKFLOW_FILE + '/dispatches';
  let code, body;
  try {
    const resp = UrlFetchApp.fetch(url, {
      method: 'post',
      contentType: 'application/json',
      headers: { Authorization: 'Bearer ' + p.GITHUB_TOKEN, Accept: 'application/vnd.github+json', 'X-GitHub-Api-Version': '2022-11-28' },
      payload: JSON.stringify({ ref: p.GITHUB_REF || 'main', inputs: { category: category, city: city, min_score: minScore, run_id: runId } }),
      muteHttpExceptions: true
    });
    code = resp.getResponseCode();
    body = resp.getContentText();
  } catch (err) {
    code = 0;
    body = String(err);
  }
  if (code === 204) {
    setStatus_(sh, 'Queued', category + ' / ' + city + ' / score ' + minScore, runId);
  } else {
    const hint = code === 401 || code === 403 ? 'Token problem. Run Leads > Setup again.' : code === 404 ? 'Repository or workflow not found.' : code === 422 ? 'Workflow not ready on this branch.' : '';
    setStatus_(sh, 'Failed: GitHub answered ' + code, (hint + ' ' + String(body).replace(/\s+/g, ' ')).trim().substring(0, 150), runId);
  }
  if (interactive) SpreadsheetApp.getActive().toast('Status: ' + sh.getRange('B8').getValue());
}
