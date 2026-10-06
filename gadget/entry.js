// frida-gadget 17 不再内置 Java 桥，这里把 frida-java-bridge 挂回全局。
const mod = require('frida-java-bridge');
const bridge = mod && typeof mod.perform === 'function' ? mod : (mod && mod.default ? mod.default : mod);
globalThis.Java = bridge;
require('./full_play.js');
require('./mpv_tune.js');
