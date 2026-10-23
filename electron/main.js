const { app, BrowserWindow, ipcMain, dialog } = require('electron');
const path = require('path');
const http = require('http');
const { spawn } = require('child_process');

let mainWindow;
let pythonProcess = null;

function checkBackendRunning(callback) {
  const req = http.get('http://127.0.0.1:8890/api/status', (res) => {
    if (res.statusCode === 200) {
      callback(true);
    } else {
      callback(false);
    }
  });
  req.on('error', () => {
    callback(false);
  });
  req.setTimeout(800, () => {
    req.destroy();
    callback(false);
  });
}

function startBackend() {
  checkBackendRunning((running) => {
    if (running) {
      console.log('[Electron] Backend server is already running on port 8890.');
      createWindow();
      return;
    }

    console.log('[Electron] Starting Python backend server on port 8890...');
    const backendPath = path.join(__dirname, '..', 'backend', 'server.py');
    pythonProcess = spawn('python', [backendPath], {
      cwd: path.join(__dirname, '..', 'backend'),
      stdio: 'pipe'
    });

    pythonProcess.stdout.on('data', (data) => {
      console.log(`[Python Backend] ${data}`);
    });

    pythonProcess.stderr.on('data', (data) => {
      console.error(`[Python Backend Err] ${data}`);
    });

    // Wait 1.5s then create window
    setTimeout(createWindow, 1500);
  });
}

function createWindow() {
  if (mainWindow) return;

  mainWindow = new BrowserWindow({
    width: 1560,
    height: 960,
    minWidth: 1200,
    minHeight: 700,
    title: 'DeepSeek-Antigravity IDE',
    backgroundColor: '#09090b',
    webPreferences: {
      preload: path.join(__dirname, 'preload.js'),
      nodeIntegration: false,
      contextIsolation: true,
      webSecurity: false
    }
  });

  const indexPath = path.join(__dirname, '..', 'frontend', 'index.html');
  mainWindow.loadFile(indexPath);

  mainWindow.on('closed', () => {
    mainWindow = null;
  });
}

app.whenReady().then(() => {
  startBackend();

  app.on('activate', () => {
    if (BrowserWindow.getAllWindows().length === 0) createWindow();
  });
});

app.on('window-all-closed', () => {
  if (pythonProcess) {
    try {
      pythonProcess.kill();
    } catch (e) {}
  }
  if (process.platform !== 'darwin') app.quit();
});

ipcMain.handle('select-directory', async () => {
  const result = await dialog.showOpenDialog(mainWindow, {
    properties: ['openDirectory']
  });
  return result.filePaths[0];
});
