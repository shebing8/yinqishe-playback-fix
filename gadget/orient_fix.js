/* 全屏方向自适应 —— 竖屏视频竖屏全屏、横屏视频横屏全屏
 *
 * 根因：media_kit_video 的 defaultEnterNativeFullscreen 无条件
 *   SystemChrome.setPreferredOrientations([landscapeLeft, landscapeRight])
 *   → PlatformPlugin → Activity.setRequestedOrientation(SCREEN_ORIENTATION_SENSOR_LANDSCAPE=6)
 * 竖屏视频进全屏被横过来，画面只占中间一条，上下大片黑边。
 * 退出全屏走 setPreferredOrientations([]) → UNSPECIFIED(-1)，与横竖屏族不重叠。
 *
 * 修复：Java 层拦 Activity.setRequestedOrientation(int)。请求是横屏族（0/6/8/11）
 * 时，用当前 mpv handle 读 width/height（按 video-params/rotate 修正显示方向），
 * 视频是竖的（高>宽）就改写成对应竖屏族（1/7/9/12）；读不到宽高一律放行。
 */

var OF_LOG = "/data/data/com.example.yinqishe_app/gadget_play.log";
var F_INT64 = 4, F_STRING = 1;

/* Android ActivityInfo.SCREEN_ORIENTATION_* */
var LAND2PORT = { 0: 1, 6: 7, 8: 9, 11: 12 };   // landscape 族 -> 同语义竖屏族
var FRESH_MS = 5000;                             // handle 最后一次 poll 事件的新鲜度上限
var CACHE_MS = 30000;                            // 宽高缓存兜底时效

var f_get = null, f_free = null;
var dimCache = null;                             // {w, h, ts}
var lastDecision = "";
var said = {};

function olog(s) {
  var line = new Date().toISOString().substring(11, 19) + " " + s;
  console.log("[orient] " + line);
  try {
    var f = new File(OF_LOG, "a");
    f.write(line + "\n");
    f.flush();
    f.close();
  } catch (e) {}
}

function mkGet() {
  if (f_get) return true;
  try {
    var mod = Process.findModuleByName("libmpv.so");
    if (!mod) return false;
    var mk = function (sym, ret, types) {
      var a = mod.findExportByName(sym);
      return a === null ? null : new NativeFunction(a, ret, types);
    };
    f_get = mk("mpv_get_property", "int", ["pointer", "pointer", "int", "pointer"]);
    f_free = mk("mpv_free", "void", ["pointer"]);
    return !!f_get;
  } catch (e) {
    return false;
  }
}

function readInt(handle, name) {
  var np = Memory.alloc(8);
  var sp = Memory.alloc(8);
  try {
    if (f_get(handle, Memory.allocUtf8String(name), F_INT64, np) === 0) return np.readS64();
    if (f_get(handle, Memory.allocUtf8String(name), F_STRING, sp) === 0) {
      var p = sp.readPointer();
      if (p.isNull()) return null;
      var v = parseFloat(p.readUtf8String());
      f_free(p);
      return isNaN(v) ? null : v;
    }
  } catch (e) {}
  return null;
}

/* 当前视频显示宽高；读不到用 30s 内缓存兜底，再没有就返回 null（调用方放行） */
function videoDims() {
  var cur = globalThis.__mpvCur;
  if (mkGet() && cur && cur.ptr && !cur.ptr.isNull() && (Date.now() - cur.ts) < FRESH_MS) {
    var w = readInt(cur.ptr, "width");
    var h = readInt(cur.ptr, "height");
    if (w > 0 && h > 0) {
      var rot = readInt(cur.ptr, "video-params/rotate");
      if (rot === 90 || rot === 270) { var t = w; w = h; h = t; }
      dimCache = { w: w, h: h, ts: Date.now() };
      return dimCache;
    }
  }
  if (dimCache && (Date.now() - dimCache.ts) < CACHE_MS) return dimCache;
  return null;
}

function fixOrientation(o) {
  if (!(o in LAND2PORT)) return o;
  var d = videoDims();
  if (d === null) return o;
  if (d.h > d.w) {
    var to = LAND2PORT[o];
    var msg = "方向请求 " + o + " 但视频 " + d.w + "x" + d.h + " 是竖屏 -> 改为 " + to;
    if (msg !== lastDecision) { olog(msg); lastDecision = msg; }
    return to;
  }
  var ok = "方向请求 " + o + "，视频 " + d.w + "x" + d.h + " 横屏，放行";
  if (ok !== lastDecision) { olog(ok); lastDecision = ok; }
  return o;
}

function installJava() {
  Java.perform(function () {
    var Activity = Java.use("android.app.Activity");
    Activity.setRequestedOrientation.overload("int").implementation = function (o) {
      try {
        o = fixOrientation(o);
      } catch (e) {
        if (!said.fix) { olog("fixOrientation 异常: " + e); said.fix = true; }
      }
      return this.setRequestedOrientation(o);
    };
    olog("方向自适应 hook 已装（竖屏视频全屏不再转横）");
  });
}

(function waitJava(t) {
  try {
    if (typeof Java === "undefined" || typeof Java.perform !== "function") throw new Error("Java 桥未就绪");
    installJava();
    return;
  } catch (e) {
    if (t >= 100) { olog("Java 桥一直不可用，方向修复未装: " + e); return; }
    setTimeout(function () { waitJava(t + 1); }, 100);
  }
})(0);
