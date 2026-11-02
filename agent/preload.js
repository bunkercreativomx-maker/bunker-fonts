const { contextBridge, ipcRenderer } = require('electron');
contextBridge.exposeInMainWorld('bunkerAgent', {
  sync: () => ipcRenderer.invoke('bf:sync'),
  status: () => ipcRenderer.invoke('bf:status'),
  getConfig: () => ipcRenderer.invoke('bf:getConfig'),
  saveConfig: c => ipcRenderer.invoke('bf:saveConfig', c),
});
