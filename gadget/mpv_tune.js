/* 播放缓冲修复 v4 —— 按 handle 应用 + 周期回读纠偏
 *
 * 实测（医馆笑传 20787，720p h264 30fps，码率约 0.65MB/s，同一场播放内切换）：
 *   app 把前向缓冲压在 readahead≈1s、demuxer-max-bytes≈32MB，demuxer 缓冲中位只有 2.5s，
 *   15% 采样 <1s，2 次 underrun、8 次进度不前进；运行中改成 readahead=30 + 96MB 后
 *   中位缓冲 101s、underrun 0。
 *
 * v2 教训：兜底只作用于脚本首次看到的 handle，app 每个视频新建 handle → 新 handle 仍是 1s。
 * v3 教训：app 建 handle 的早期（mpv_initialize 之前）set_property 返回 rc=0，
 *          但播放中回读 demuxer-readahead-secs 仍是 1.000000 —— 早期写入不会生效。
 *          所以既在 handle 出现时立刻设，又在事件循环里周期回读、发现被改回就再设。
 */

var TUNE_LOG = "/data/data/com.example.yinqishe_app/gadget_mpv.log";

var F_STRING = 1, F_INT64 = 4, F_DOUBLE = 5;
var OVERRIDE = {
  "demuxer-readahead-secs": "30",
  "demuxer-max-bytes": "100663296",      // 96 MiB 前向，约 150s 码率余量
  "demuxer-max-back-bytes": "8388608",   // 后向留小，省下的都是前向空间
  "network-timeout": "15"                // app 给 5s，慢一点的分片就被掐断重连
};

var said = {};            // 日志去重
globalThis.__mpvTune = OVERRIDE;   // 给命令通道留个改目标的口子，便于 A/B
var H = null;
var f_setProp = null, f_get = null, f_free = null;
var applying = 0, lastEnforce = 0, lastStat = 0;
var ENFORCE_MS = 1000;
var STAT_MS = 10000;

function tlog(s) {
  var line = new Date().toISOString().substring(11, 19) + " [tune] " + s;
  console.log(line);
  try {
    var fh = new File(TUNE_LOG, "a");
    fh.write(line + "\n");
    fh.flush();
    fh.close();
  } catch (e) {}
}

function nameOf(p) {
  try {
    return p.isNull() ? null : p.readUtf8String();
  } catch (e) {
    return null;
  }
}

function readNum(handle, name) {
  var np = Memory.alloc(8);
  var sp = Memory.alloc(8);
  applying++;
  try {
    if (f_get(handle, Memory.allocUtf8String(name), F_INT64, np) === 0) return np.readS64();
    if (f_get(handle, Memory.allocUtf8String(name), F_DOUBLE, np) === 0) return np.readDouble();
    if (f_get(handle, Memory.allocUtf8String(name), F_STRING, sp) === 0) {
      var p = sp.readPointer();
      if (p.isNull()) return null;
      var s = p.readUtf8String();
      f_free(p);
      // 字节类选项的字符串形式可能带 MiB/KiB 后缀
      var m = /^([\d.]+)\s*([KMGT]?i?B)?$/i.exec(String(s).trim());
      if (!m) return parseFloat(s);
      var mult = { "": 1, K: 1024, M: 1048576, G: 1073741824, T: 1099511627776 };
      var pre = (m[2] || "").charAt(0).toUpperCase();
      return parseFloat(m[1]) * (mult[pre] || 1);
    }
    return null;
  } catch (e) {
    return null;
  } finally {
    applying--;
  }
}

function setOne(handle, name, val, tag, once) {
  applying++;
  var rc;
  try {
    rc = f_setProp(handle, Memory.allocUtf8String(name), Memory.allocUtf8String(val));
  } catch (e) {
    rc = "ex";
  }
  applying--;
  var k = tag + ":" + name;
  if (!once || !said[k]) { tlog(tag + " set " + name + "=" + val + " rc=" + rc); said[k] = true; }
  return rc;
}

/* app 下发的窄缓冲参数，在它写入 mpv 之前就地换掉 */
function clampArgs(args, tag) {
  var name = nameOf(args[1]);
  if (name === null || !(name in OVERRIDE)) return;
  var want = OVERRIDE[name];
  var old = nameOf(args[2]);
  if (old === want) return;
  this.keep = Memory.allocUtf8String(want);
  args[2] = this.keep;
  var k = "clamp:" + name;
  if (!said[k]) { tlog("拦截 " + tag + " " + name + " " + old + " -> " + want); said[k] = true; }
}

function applyHandle(handle, tag) {
  if (!f_setProp || handle.isNull()) return;
  Object.keys(OVERRIDE).forEach(function (name) {
    setOne(handle, name, OVERRIDE[name], tag, true);
  });
  tlog("回读 " + Object.keys(OVERRIDE).map(function (n) {
    var v = readNum(handle, n);
    return n + "=" + (v === null ? "?" : v);
  }).join(" "));
  var chk = Object.keys(OVERRIDE).map(function (name) {
    var v = readNum(handle, name);
    return name.replace("demuxer-", "") + "=" + (v === null ? "?" : v);
  });
  tlog("回读 " + handle + ": " + chk.join(" "));
}

/* 回读一遍：值不对就再设，直到对为止（app 会在新建 handle 时把我们早期的写入丢掉） */
function enforce(handle) {
  Object.keys(OVERRIDE).forEach(function (name) {
    var want = parseFloat(OVERRIDE[name]);
    var cur = readNum(handle, name);
    if (cur === null || isNaN(cur)) return;
    if (Math.abs(cur - want) <= Math.abs(want) * 0.02) return;   // 已经对了
    setOne(handle, name, OVERRIDE[name], "回读 " + cur + " 纠正", false);
  });
}

function install() {
  var mod = Process.findModuleByName("libmpv.so");
  if (!mod) return false;
  var mk = function (sym, ret, types) {
    var a = mod.findExportByName(sym);
    return a === null ? null : new NativeFunction(a, ret, types);
  };
  f_setProp = mk("mpv_set_property_string", "int", ["pointer", "pointer", "pointer"]);
  f_get = mk("mpv_get_property", "int", ["pointer", "pointer", "int", "pointer"]);
  f_free = mk("mpv_free", "void", ["pointer"]);
  if (!f_setProp || !f_get || !f_free) { tlog("缺导出，无法修复"); return true; }

  ["mpv_set_property_string", "mpv_set_option_string"].forEach(function (sym) {
    var a = mod.findExportByName(sym);
    if (a === null) { tlog("无导出 " + sym); return; }
    Interceptor.attach(a, {
      onEnter: function (args) { clampArgs.call(this, args, sym); }
    });
  });

  Interceptor.attach(mod.findExportByName("mpv_wait_event"), {
    onEnter: function (args) {
      var h = args[0];
      if (h.isNull()) return;
      var key = h.toString();
      if (key !== H) {
        H = key;
        tlog("发现 handle " + key);
        applyHandle(h, "初设");
      }
      var now = Date.now();
      if (now - lastEnforce >= ENFORCE_MS) {
        lastEnforce = now;
        enforce(h);
      }
      /* 每 10s 报一次时长与已缓冲秒数：duration>20 即正片（试看只有 12s） */
      if (now - lastStat >= STAT_MS) {
        lastStat = now;
        var dur = readNum(h, "duration");
        var cache = readNum(h, "demuxer-cache-duration");
        tlog("stat duration=" + (dur === null ? "?" : dur.toFixed(1)) +
             " cache=" + (cache === null ? "?" : cache.toFixed(1)) + "s");
      }
    }
  });
  tlog("缓冲修复 v4 hook 已装 base=" + mod.base);
  return true;
}

tlog("缓冲修复 v4 启动 pid=" + Process.id);
(function wait(t) {
  if (install()) return;
  if (t < 3000) setTimeout(function () { wait(t + 1); }, 50);
  else tlog("等不到 libmpv.so");
})(0);
