'use strict';
const { contextBridge, ipcRenderer } = require('electron');

const IN = new Set(['snapshot', 'engine', 'overlay:state', 'overlay:editing', 'roi:init']);
const OUT = new Set(['ui:ready', 'overlay:toggle', 'overlay:edit', 'overlay:size', 'engine:cmd',
                     'roi:open', 'roi:done', 'roi:cancel']);

contextBridge.exposeInMainWorld('kfl', {
  on(channel, fn) {
    if (IN.has(channel)) ipcRenderer.on(channel, (_e, data) => fn(data));
  },
  send(channel, data) {
    if (OUT.has(channel)) ipcRenderer.send(channel, data);
  },
});
