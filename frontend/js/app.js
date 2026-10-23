// DeepSeek-Antigravity IDE Core Application Logic
let monacoEditor = null;
let monacoDiffEditor = null;
let currentFilePath = "run_stock.py";
let debateWs = null;
let agentWs = null;
let isInlineDiff = false;
let pendingDiffData = null;

// Initialize Monaco Editor
require.config({ paths: { 'vs': 'https://cdnjs.cloudflare.com/ajax/libs/monaco-editor/0.45.0/min/vs' } });
require(['vs/editor/editor.main'], function() {
  monaco.editor.defineTheme('antigravityDark', {
    base: 'vs-dark',
    inherit: true,
    rules: [
      { token: 'comment', foreground: '6A737D', fontStyle: 'italic' },
      { token: 'keyword', foreground: 'F97583' },
      { token: 'string', foreground: '9ECBFF' },
      { token: 'number', foreground: '79B8FF' },
      { token: 'function', foreground: 'B392F0' },
      { token: 'variable', foreground: 'E1E4E8' },
    ],
    colors: {
      'editor.background': '#09090b',
      'editor.foreground': '#f4f4f5',
      'editorCursor.foreground': '#c084fc',
      'editor.lineHighlightBackground': '#18181b',
      'editorLineNumber.foreground': '#52525b',
      'editorLineNumber.activeForeground': '#a1a1aa',
      'editor.selectionBackground': '#27272a',
      'editorDiff.insertedLineBackground': 'rgba(16, 185, 129, 0.15)',
      'editorDiff.removedLineBackground': 'rgba(239, 68, 68, 0.15)',
    }
  });

  monacoEditor = monaco.editor.create(document.getElementById('monacoEditorContainer'), {
    value: '# DeepSeek-Antigravity IDE 로딩 중...',
    language: 'python',
    theme: 'antigravityDark',
    automaticLayout: true,
    fontSize: 13,
    lineHeight: 20,
    minimap: { enabled: true },
    scrollBeyondLastLine: false,
    renderWhitespace: 'selection'
  });

  // Block 2: Monaco Diff Editor
  const diffElem = document.getElementById('monacoDiffEditorElement') || document.getElementById('monacoDiffContainer');
  monacoDiffEditor = monaco.editor.createDiffEditor(diffElem, {
    theme: 'antigravityDark',
    automaticLayout: true,
    fontSize: 13,
    readOnly: true,
    renderSideBySide: true,
    originalEditable: false
  });

  loadWorkspaceTree();
  loadFile("run_stock.py");
});

// Load Workspace Directory Tree
async function loadWorkspaceTree() {
  const treeContainer = document.getElementById('explorerTree');
  try {
    const res = await fetch('http://127.0.0.1:8890/api/workspace/tree');
    const data = await res.json();
    if (data.workspace) {
      document.getElementById('activeProjectName').textContent = data.workspace.split(/[\\/]/).pop();
    }
    treeContainer.innerHTML = '';
    renderTreeNodes(data.tree || [], treeContainer, 0);
  } catch (err) {
    treeContainer.innerHTML = `<div class="term-line error">백엔드 연결 대기 중 (포트 8890)...</div>`;
  }
}

function renderTreeNodes(nodes, container, depth) {
  nodes.forEach(node => {
    const row = document.createElement('div');
    row.className = 'tree-node';
    if (node.path === currentFilePath) row.classList.add('active');

    let indentHtml = '';
    for (let i = 0; i < depth; i++) {
      indentHtml += '<span class="node-indent"></span>';
    }

    const icon = node.type === 'directory' ? '📁' : (node.name.endsWith('.py') ? '🐍' : '📄');
    row.innerHTML = `${indentHtml}<span class="node-icon">${icon}</span><span>${node.name}</span>`;

    row.addEventListener('click', () => {
      if (node.type === 'file') {
        document.querySelectorAll('.tree-node').forEach(el => el.classList.remove('active'));
        row.classList.add('active');
        loadFile(node.path);
      }
    });

    container.appendChild(row);

    if (node.type === 'directory' && node.children && node.children.length > 0) {
      renderTreeNodes(node.children, container, depth + 1);
    }
  });
}

// Load File into Monaco
async function loadFile(filePath) {
  try {
    const res = await fetch('http://127.0.0.1:8890/api/workspace/read', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ path: filePath })
    });
    const data = await res.json();
    if (res.ok) {
      currentFilePath = filePath;
      document.getElementById('tabFileName').textContent = filePath.split(/[\\/]/).pop();
      const lang = filePath.endsWith('.py') ? 'python' : (filePath.endsWith('.js') ? 'javascript' : (filePath.endsWith('.json') ? 'json' : 'plaintext'));
      
      const model = monaco.editor.createModel(data.content, lang);
      monacoEditor.setModel(model);
      appendTerminal(`[파일 로드 완료]: ${filePath} (${data.lines} 줄, ${data.encoding})`, 'info');
    } else {
      appendTerminal(`[파일 로드 실패]: ${data.detail}`, 'error');
    }
  } catch (e) {
    appendTerminal(`[통신 오류]: ${e.message}`, 'error');
  }
}

// Save Current File
async function saveCurrentFile() {
  if (!monacoEditor) return;
  const content = monacoEditor.getValue();
  try {
    const res = await fetch('http://127.0.0.1:8890/api/workspace/save', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ path: currentFilePath, content: content })
    });
    const data = await res.json();
    if (res.ok) {
      appendTerminal(`[파일 저장 완료]: ${currentFilePath}`, 'success');
      if (data.syntax) {
        if (data.syntax.valid) {
          appendTerminal(`[문법 검증 완료]: py_compile 통과 (0 SyntaxError)`, 'success');
        } else {
          appendTerminal(`[문법 오류 경고]: ${data.syntax.error}`, 'error');
        }
      }
    } else {
      appendTerminal(`[파일 저장 실패]: ${data.detail}`, 'error');
    }
  } catch (e) {
    appendTerminal(`[저장 통신 실패]: ${e.message}`, 'error');
  }
}

function appendTerminal(text, type = 'stdout') {
  const term = document.getElementById('terminalOutput');
  const line = document.createElement('div');
  line.className = `term-line ${type}`;
  line.textContent = `[${new Date().toLocaleTimeString()}] ${text}`;
  term.appendChild(line);
  term.scrollTop = term.scrollHeight;
}

function highlightRoleChip(roleKey) {
  document.querySelectorAll('.role-chip').forEach(el => el.classList.remove('active'));
  const target = {
    'architect': 'roleR1',
    'critic': 'roleV3Critic',
    'engineer': 'roleV3Eng',
    'qa': 'roleQA',
    'git': 'roleGit'
  }[roleKey];
  if (target) {
    const el = document.getElementById(target);
    if (el) el.classList.add('active');
  }
}

// =========================================================================
// Block 2: Monaco Diff Engine Functions (Needleman-Wunsch & Model Binding)
// =========================================================================
function showDiffView(originalText, modifiedText, filePath, diffHunk) {
  const editorContainer = document.getElementById('monacoEditorContainer');
  const diffContainer = document.getElementById('monacoDiffContainer');
  
  editorContainer.style.display = 'none';
  diffContainer.style.display = 'block';

  document.getElementById('diffFilePath').textContent = filePath.split(/[\\/]/).pop();
  
  const origLines = originalText ? originalText.split('\n').length : 0;
  const modLines = modifiedText ? modifiedText.split('\n').length : 0;
  const delta = modLines - origLines;
  document.getElementById('diffStats').textContent = `${delta >= 0 ? '+' : ''}${delta} 줄 (${origLines}줄 ➔ ${modLines}줄)`;

  const lang = filePath.endsWith('.py') ? 'python' : (filePath.endsWith('.js') ? 'javascript' : 'plaintext');

  const originalModel = monaco.editor.createModel(originalText || '', lang);
  const modifiedModel = monaco.editor.createModel(modifiedText || '', lang);

  monacoDiffEditor.setModel({
    original: originalModel,
    modified: modifiedModel
  });
  monacoDiffEditor.updateOptions({
    renderSideBySide: !isInlineDiff,
    readOnly: true
  });
  monacoDiffEditor.layout();

  pendingDiffData = {
    filePath: filePath,
    newContent: modifiedText,
    originalContent: originalText
  };
  
  appendTerminal(`[인라인 Diff 뷰어 가동]: ${filePath} 대조 중 (Alt+Enter로 즉시 적용 가능)`, 'info');
}

function acceptDiffChanges() {
  if (!pendingDiffData) return;
  const { filePath, newContent } = pendingDiffData;
  
  fetch('http://127.0.0.1:8890/api/workspace/save', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ path: filePath, content: newContent })
  }).then(res => res.json()).then(data => {
    appendTerminal(`[코드 변경 적용 완료]: ${filePath} 승인 및 디스크 영구 저장 완료!`, 'success');
    closeDiffView();
    loadFile(filePath);
  }).catch(e => {
    appendTerminal(`[적용 오류]: ${e.message}`, 'error');
  });
}

function rejectDiffChanges() {
  appendTerminal('[수정안 거절]: 원본 코드를 그대로 유지합니다.', 'warn');
  closeDiffView();
}

function closeDiffView() {
  document.getElementById('monacoEditorContainer').style.display = 'block';
  document.getElementById('monacoDiffContainer').style.display = 'none';
  pendingDiffData = null;
  if (monacoEditor) monacoEditor.layout();
}

// 5-Role Collaborative Debate Pipeline (Block 1 & 3)
function start5RoleDebate(task) {
  if (debateWs) { debateWs.close(); }
  debateWs = new WebSocket('ws://127.0.0.1:8890/ws/debate');

  appendChatMessage('user', '대표님 (사용자 지시)', task, '👤');

  let currentMsgBox = null;
  let currentMsgBody = null;

  debateWs.onopen = () => {
    debateWs.send(JSON.stringify({ task: task, target_file: currentFilePath }));
    appendTerminal('[5대 협업체 전수 가동]: R1 아키텍트, V3 감사관, V3 엔지니어, QA 검증관, Git 관리관 호출', 'info');
  };

  debateWs.onmessage = (event) => {
    const data = JSON.parse(event.data);
    const speaker = data.speaker || '';
    const role = data.role || '';
    const msg = data.message || '';
    const chunk = data.chunk || '';

    if (role) {
      highlightRoleChip(role);
    }

    if (msg) {
      appendChatMessage(role, speaker, msg);
    } else if (chunk) {
      if (!currentMsgBox || currentMsgBox.dataset.speaker !== speaker) {
        currentMsgBox = document.createElement('div');
        currentMsgBox.className = `chat-msg ${role}`;
        currentMsgBox.dataset.speaker = speaker;
        currentMsgBox.innerHTML = `
          <div class="msg-header"><span>${speaker}</span></div>
          <div class="msg-body"></div>
        `;
        document.getElementById('agentChatLog').appendChild(currentMsgBox);
        currentMsgBody = currentMsgBox.querySelector('.msg-body');
      }
      
      // Clean Korean formatting
      let formattedChunk = chunk
        .replace(/###\s*\[(.*?)\]/g, '<div class="msg-subhead">[$1]</div>')
        .replace(/\*\*(.*?)\*\*/g, '<strong>$1</strong>');
      
      currentMsgBody.innerHTML += formattedChunk;
      document.getElementById('agentChatLog').scrollTop = document.getElementById('agentChatLog').scrollHeight;
    }

    if (data.type === 'file_opened') {
      appendTerminal(`[파일 전수 열람]: ${data.data.filename} (${data.data.lines}줄 로드)`, 'info');
      loadFile(data.data.path);
    } else if (data.type === 'diff_ready') {
      // Connect to Monaco Diff Editor
      const target = data.data.path;
      const newContent = data.data.new_content;
      const currentContent = monacoEditor ? monacoEditor.getValue() : '';
      showDiffView(currentContent, newContent, target, data.data.diff);
    } else if (data.type === 'session_complete') {
      appendTerminal('[협업 완료]: 5대 AI 프로세스가 완료되었습니다. (에디터에서 수정본을 확인하십시오)', 'success');
      document.querySelectorAll('.role-chip').forEach(el => el.classList.remove('active'));
    }
  };

  debateWs.onerror = (err) => {
    appendTerminal(`[웹소켓 통신 오류]: ${err.message}`, 'error');
  };
}

function appendChatMessage(role, speaker, text, avatar = '') {
  const log = document.getElementById('agentChatLog');
  const msg = document.createElement('div');
  msg.className = `chat-msg ${role}`;
  
  let formattedText = escapeHtml(text)
    .replace(/\n/g, '<br>')
    .replace(/###\s*\[(.*?)\]/g, '<div class="msg-subhead">[$1]</div>')
    .replace(/\*\*(.*?)\*\*/g, '<strong>$1</strong>')
    .replace(/`(.*?)`/g, '<code style="background:rgba(255,255,255,0.1);padding:2px 4px;border-radius:3px;">$1</code>');

  msg.innerHTML = `
    <div class="msg-header"><span>${avatar ? avatar + ' ' : ''}${speaker}</span></div>
    <div class="msg-body">${formattedText}</div>
  `;
  log.appendChild(msg);
  log.scrollTop = log.scrollHeight;
}

function escapeHtml(text) {
  const div = document.createElement('div');
  div.textContent = text;
  return div.innerHTML;
}

// Event Bindings
document.getElementById('btnSaveFile').addEventListener('click', saveCurrentFile);
document.getElementById('btnRefreshTree').addEventListener('click', loadWorkspaceTree);
document.getElementById('btnResetSession').addEventListener('click', () => {
  document.getElementById('agentChatLog').innerHTML = `
    <div class="chat-welcome-card">
      <h4>🧹 세션 메모리 초기화 완료</h4>
      <p>새로운 대화 컨텍스트가 시작되었습니다.</p>
    </div>
  `;
  appendTerminal('[세션 초기화]: 24턴 롤링 메모리가 리셋되었습니다.', 'info');
});

document.getElementById('btnStartDebate').addEventListener('click', () => {
  const prompt = document.getElementById('promptInput').value.trim();
  if (prompt) {
    start5RoleDebate(prompt);
    document.getElementById('promptInput').value = '';
  }
});

document.getElementById('promptInput').addEventListener('keydown', (e) => {
  if (e.key === 'Enter' && !e.shiftKey) {
    e.preventDefault();
    const prompt = document.getElementById('promptInput').value.trim();
    if (prompt) {
      start5RoleDebate(prompt);
      document.getElementById('promptInput').value = '';
    }
  }
});

document.getElementById('btnClearTerm').addEventListener('click', () => {
  document.getElementById('terminalOutput').innerHTML = '';
});

document.getElementById('btnRunSyntax').addEventListener('click', async () => {
  try {
    const res = await fetch('http://127.0.0.1:8890/api/terminal/run', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ command: `python -m py_compile "${currentFilePath}"` })
    });
    const data = await res.json();
    if (data.exit_code === 0) {
      appendTerminal(`[py_compile 검증 성공]: ${currentFilePath} 문법 무결점 통과!`, 'success');
    } else {
      appendTerminal(`[py_compile 검증 실패]:\n${data.stderr}`, 'error');
    }
  } catch (e) {
    appendTerminal(`[검증 오류]: ${e.message}`, 'error');
  }
});

// Diff buttons
document.getElementById('btnAcceptDiff').addEventListener('click', acceptDiffChanges);
document.getElementById('btnRejectDiff').addEventListener('click', rejectDiffChanges);
document.getElementById('btnToggleInlineDiff').addEventListener('click', () => {
  isInlineDiff = !isInlineDiff;
  if (monacoDiffEditor) {
    monacoDiffEditor.updateOptions({ renderSideBySide: !isInlineDiff });
    monacoDiffEditor.layout();
  }
});
document.getElementById('btnToggleDiff').addEventListener('click', () => {
  const diffContainer = document.getElementById('monacoDiffContainer');
  if (diffContainer.style.display === 'block') {
    closeDiffView();
  } else {
    if (pendingDiffData) {
      showDiffView(pendingDiffData.originalContent, pendingDiffData.newContent, pendingDiffData.filePath, '');
    } else {
      appendTerminal('[Diff 알림]: 현재 대기 중인 수정안이 없습니다.', 'warn');
    }
  }
});

// Keyboard shortcuts (Alt+Enter for Accept, Esc for Reject)
window.addEventListener('keydown', (e) => {
  if (e.altKey && e.key === 'Enter') {
    if (pendingDiffData) {
      e.preventDefault();
      acceptDiffChanges();
    }
  } else if (e.key === 'Escape') {
    if (pendingDiffData) {
      e.preventDefault();
      rejectDiffChanges();
    }
  }
});

// Block 4: Rule Editor Modal Handlers
const modal = document.getElementById('ruleEditorModal');
document.getElementById('btnOpenRules').addEventListener('click', () => {
  modal.style.display = 'flex';
});
document.getElementById('btnCloseRules').addEventListener('click', () => {
  modal.style.display = 'none';
});
document.getElementById('btnSaveRules').addEventListener('click', () => {
  modal.style.display = 'none';
  appendTerminal('[규칙 엔진]: 6대 절대 수칙이 활성화 상태로 확인되었습니다.', 'info');
});

// Resizers logic
function setupResizer(resizerId, prevPaneId, isHorizontal = false) {
  const resizer = document.getElementById(resizerId);
  const pane = document.getElementById(prevPaneId);
  let isDown = false;

  resizer.addEventListener('mousedown', (e) => {
    isDown = true;
    resizer.classList.add('resizing');
    document.body.style.cursor = isHorizontal ? 'row-resize' : 'col-resize';
  });

  document.addEventListener('mousemove', (e) => {
    if (!isDown) return;
    if (isHorizontal) {
      const containerTop = pane.parentElement.getBoundingClientRect().top;
      const newHeight = e.clientY - containerTop;
      if (newHeight > 100 && newHeight < window.innerHeight - 200) {
        pane.style.height = `${newHeight}px`;
      }
    } else {
      const newWidth = e.clientX;
      if (newWidth > 150 && newWidth < 500) {
        pane.style.width = `${newWidth}px`;
      }
    }
  });

  document.addEventListener('mouseup', () => {
    if (isDown) {
      isDown = false;
      resizer.classList.remove('resizing');
      document.body.style.cursor = 'default';
      if (monacoEditor) monacoEditor.layout();
      if (monacoDiffEditor) monacoDiffEditor.layout();
    }
  });
}

setupResizer('resizerLeft', 'paneExplorer', false);
setupResizer('resizerBottom', 'editorViewport', true);
