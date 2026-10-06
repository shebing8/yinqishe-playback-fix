/* 金币视频在线播正片 —— frida-gadget 常驻版
 * 原语：authPath 只按 auth_key 第三段首位 flag 决定返回 12 秒还是完整列表，且 key 不绑定 path。
 * 所以：拦下客户端拿到的 0 前缀 authKey，原地换成一张合法签名的 1 前缀 key（等长 86 字符）。
 * 1 前缀 key 来源：
 *   1) 被动缓存 app 自己见过的 1 前缀 key；
 *   2) 用 app 当前账号向 API 现签免费片；
 *   3) 账号配额（freeWatches=50）用完时，POST /api/user/traveler 换一个带全新额度的游客账号来签。
 *      flag 由账号的 remainingWatches 决定，与 path 无关，所以用别的账号的 key 一样能解出完整列表。
 */

var PKG_DATA = "/data/data/com.example.yinqishe_app";
var LOG = PKG_DATA + "/gadget_play.log";

var API_HOST = "https://ly0w9yi.9p9d6bo0.work";     // 会被 app 自己发出的 authPath 域名自动校正
var FREE_IDS = ["20787", "20954"];                   // price=0，账号有额度时服务端给 1 前缀 key
var DEV_ID = "6a3ab90e5ee9c00f83791853136edcf28954113a";
var BAK_FILE = PKG_DATA + "/gadget_bak.txt";         // 备用游客落盘，重启进程不再重复开号
var MINT_FILE = PKG_DATA + "/gadget_mint.txt";       // 换号时刻落盘，节流跨进程生效
var KEY_TTL = 420;                                   // 秒；auth_key 首段是签发时刻，留足 CDN 窗口余量
var REFRESH_MS = 240000;                             // 4 分钟续一次，保证换进去的 key 永远比 KEY_TTL 新
var MINT_WINDOW = 1800;                              // 秒；换号节流窗口
var MINT_WINDOW_MAX = 6;                             // 窗口内最多换几个号，避免刷爆服务端

var OFF_READ = 0x4f6b30;        // StorageService._read
var OFF_ASSTRING = 0x4f6a1c;    // MapExt.asString

var TOKEN = null;                                    // app 当前会话 token
var BAK = null;                                      // 现签用的备用游客 {tok, dev, uid}
var goodKey = null;
var goodKeyTs = 0;
var fetching = false;
var stats = { swaps: 0, signed: 0, tokens: 0, minted: 0 };

function log(s) {
  var line = new Date().toISOString().substring(11, 19) + " " + s;
  console.log("[fullplay] " + line);
  try {
    var f = new File(LOG, "a");
    f.write(line + "\n");
    f.flush();
    f.close();
  } catch (e) {}
}

/* ---------- Dart 侧 ---------- */

function readDartStr(p) {
  try {
    var v = getTaggedObjectValue(p);
    if (v && typeof v[2] === "string") return v;
  } catch (e) {}
  return null;
}

function writeStr(v, s) {
  var obj = v[0].sub(1);
  var codes = [];
  for (var i = 0; i < s.length; i++) codes.push(s.charCodeAt(i));
  if (v[1].id === 95) {                       // TwoByteString -> UTF-16LE
    var b = [];
    codes.forEach(function (c) { b.push(c & 0xff, 0); });
    obj.add(16).writeByteArray(b);
  } else {                                    // OneByteString (cid 94)
    obj.add(16).writeByteArray(codes);
  }
}

var KEY_RE = /^(\d{10})-([0-9a-f]{32})-([01])(\d{8})-([0-9a-f]{32})$/;

function flagOf(s) {
  var m = KEY_RE.exec(s);
  return m ? m[3] : null;
}

function noteKey(s) {
  if (flagOf(s) === "1") {
    goodKey = s;
    goodKeyTs = Date.now() / 1000;
    return true;
  }
  return false;
}

function keyAlive() {
  return goodKey && (Date.now() / 1000 - goodKeyTs) < KEY_TTL;
}

/* ---------- Java 侧：签名请求 + AES 解密 ---------- */

var J = null;

function loadJava() {
  var StrictMode = Java.use("android.os.StrictMode");
  var B = Java.use("android.os.StrictMode$ThreadPolicy$Builder");
  StrictMode.setThreadPolicy(B.$new().permitAll().build());   // 允许本进程内同步网络

  J = {
    URL: Java.use("java.net.URL"),
    String: Java.use("java.lang.String"),
    MD: Java.use("java.security.MessageDigest"),
    B64: Java.use("android.util.Base64"),
    Cipher: Java.use("javax.crypto.Cipher"),
    SKey: Java.use("javax.crypto.spec.SecretKeySpec"),
    IV: Java.use("javax.crypto.spec.IvParameterSpec"),
    BAOS: Java.use("java.io.ByteArrayOutputStream"),
    byteArr: function (arr) { return Java.array("byte", arr); }
  };
}

function ensureJava() {
  if (J !== null) return true;
  if (typeof Java === "undefined") { log("Java 桥不可用"); return false; }
  try {
    Java.perform(loadJava);
    log("Java 桥就绪");
    return true;
  } catch (e) {
    log("loadJava 失败: " + e + " | perform=" + typeof Java.perform +
        " keys=" + Object.keys(Java).slice(0, 8).join(","));
    return false;
  }
}

function asciiBytes(s) {
  var a = [];
  for (var i = 0; i < s.length; i++) a.push(s.charCodeAt(i));
  return a;
}

function hex(md) {
  var out = "";
  for (var i = 0; i < md.length; i++) {
    var b = md[i] & 0xff;
    out += (b < 16 ? "0" : "") + b.toString(16);
  }
  return out;
}

function extractJson(s) {
  var start = s.indexOf("{");
  if (start < 0) return null;
  var depth = 0, inStr = false, esc = false;
  for (var i = start; i < s.length; i++) {
    var c = s[i];
    if (inStr) {
      if (esc) esc = false;
      else if (c === "\\") esc = true;
      else if (c === '"') inStr = false;
    } else if (c === '"') inStr = true;
    else if (c === "{") depth++;
    else if (c === "}") {
      if (--depth === 0) return s.substring(start, i + 1);
    }
  }
  return null;
}

function errStr(e) {
  var s;
  try { s = String(e); } catch (x) { return "<异常对象无法转换>"; }
  try {
    if (e && e.stack) s += " || " + String(e.stack).split("\n").slice(0, 3).join(" | ");
  } catch (x) {}
  return s;
}

function headerOf(conn, name) {
  // getHeaderField(String) 声明在 URLConnection 上，按 HttpURLConnection 取不到
  return Java.cast(conn, Java.use("java.net.URLConnection")).getHeaderField(name);
}

function openConn(u) {
  // openConnection 的声明返回类型是 URLConnection，方法要按 HttpURLConnection 取
  return Java.cast(u.openConnection(), Java.use("java.net.HttpURLConnection"));
}

function step(name, fn) {
  try {
    return fn();
  } catch (e) {
    log("step " + name + " FAIL: " + errStr(e));
    return null;
  }
}

function setupConn(conn, t, s, signer, needBody) {
  conn.setConnectTimeout(15000);
  conn.setReadTimeout(20000);
  conn.setInstanceFollowRedirects(false);
  conn.setRequestProperty("accept", "application/json;charset=UTF-8");
  conn.setRequestProperty("User-Agent", "okhttp/4.12.0");
  conn.setRequestProperty("t", t);
  conn.setRequestProperty("s", s);
  conn.setRequestProperty("aut", signer.tok);
  conn.setRequestProperty("deviceId", signer.dev);
  if (needBody) {
    conn.setRequestMethod("POST");
    conn.setDoOutput(true);
    conn.setRequestProperty("content-type", "application/json");
  }
  return conn;
}

function nonce() {
  var t = String(Date.now()) + "000";                      // 微秒
  var d = step("md5", function () {
    return J.MD.getInstance("MD5").digest(J.byteArr(asciiBytes(t.substring(3, 16))));
  });
  return d === null ? null : { t: t, s: hex(d) };
}

function sendBody(conn, body) {
  var os = step("getOutputStream", function () { return conn.getOutputStream(); });
  if (os === null) return false;
  var ok = step("write", function () {
    os.write(J.byteArr(asciiBytes(body)));
    os.flush();
    os.close();
    return true;
  });
  return ok === true;
}

function apiReq(signer, method, pathWithQuery, body) {
  var post = method === "POST";
  var u = step("url", function () { return J.URL.$new(API_HOST + pathWithQuery); });
  if (u === null) return null;
  var conn = step("openConnection", function () { return openConn(u); });
  if (conn === null) return null;
  var n1 = nonce();
  if (n1 === null) return null;
  if (step("headers", function () { return setupConn(conn, n1.t, n1.s, signer, post); }) === null) return null;
  if (post && !sendBody(conn, body || "{}")) return null;

  var code = step("getResponseCode", function () { return conn.getResponseCode(); });
  if (code === null) return null;
  log("api " + method + " " + pathWithQuery.substring(0, 34) + " 状态码 " + code);

  var rotated = step("rotated", function () { return headerOf(conn, "refresh-authorization"); });
  if (rotated && String(rotated).length > 20) {
    signer.tok = String(rotated);
    if (signer.app) TOKEN = signer.tok;
    else if (signer === BAK) saveBak();      // 备用号 token 会被服务端轮换，落盘才不会用过期值
    stats.tokens++;
    log("token 轮换（refresh-authorization）");
  }
  if (code >= 300 && code < 400) {                          // 带新 token 重试一次
    var n2 = nonce();
    var conn2 = step("retryOpen", function () {
      return setupConn(openConn(u), n2.t, n2.s, signer, post);
    });
    if (conn2 === null) return null;
    if (post && !sendBody(conn2, body || "{}")) return null;
    code = step("retryCode", function () { return conn2.getResponseCode(); });
    if (code === null) return null;
    conn = conn2;
    log("api 重试状态码 " + code);
  }

  var is = step("stream", function () { return code >= 400 ? conn.getErrorStream() : conn.getInputStream(); });
  if (is === null) { log("api 无响应流"); return null; }
  var total = 0;
  var baos = step("read", function () {
    var out = J.BAOS.$new();
    var buf = Java.array("byte", new Array(8192).fill(0));
    var n;
    while ((n = is.read(buf)) !== -1) { out.write(buf, 0, n); total += n; }
    is.close();
    return out;
  });
  if (baos === null) return null;
  return { code: code, body: J.String.$new(baos.toByteArray(), "UTF-8").toString() };
}

function decryptEncData(encData, tok) {
  var raw = J.B64.decode(encData, 0);
  var kb = J.byteArr(asciiBytes(tok.substring(2, 18)));
  var cipher = J.Cipher.getInstance("AES/CBC/PKCS7Padding");
  var init = cipher.init.overload("int", "java.security.Key", "java.security.spec.AlgorithmParameterSpec");
  init.call(cipher, 2, J.SKey.$new(kb, "AES"), J.IV.$new(kb));   // 2 = DECRYPT_MODE
  var pt = cipher.doFinal(raw);
  return J.String.$new(pt, "UTF-8").toString();
}

/* 服务端按「本次请求带的那个 token」加密响应，而 apiReq 里 token 可能已被 refresh-authorization
 * 轮换掉，所以解密必须用发出去的 sentTok，不是轮换后的 signer.tok。 */
function readInner(r, sentTok, signer, tag) {
  var outer = step(tag + "Outer", function () { return JSON.parse(r.body); });
  if (outer === null || !outer.encData) {
    log(tag + " 响应无 encData: " + r.body.substring(0, 120));
    return null;
  }
  var plain = step(tag + "Dec", function () { return decryptEncData(outer.encData, sentTok); });
  if (plain === null && signer.tok !== sentTok) {
    plain = step(tag + "Dec2", function () { return decryptEncData(outer.encData, signer.tok); });
  }
  if (plain === null) return null;
  var inner = step(tag + "Inner", function () { return JSON.parse(extractJson(plain)); });
  return inner;
}

function newDev() {
  var h = "0123456789abcdef", s = "";
  for (var i = 0; i < 40; i++) s += h.charAt(Math.floor(Math.random() * 16) % 16);
  return s;
}

function uidOf(tok) {
  try {
    // 11 = URL_SAFE | NO_PADDING | NO_WRAP，JWT payload 没有补位
    var raw = step("uidB64", function () { return J.B64.decode(String(tok).split(".")[1], 11); });
    if (raw === null) return "?";
    var m = /"sub":"?(\d+)/.exec(String(J.String.$new(raw, "UTF-8").toString()));
    return m ? m[1] : "?";
  } catch (e) {
    return "?";
  }
}

function saveBak() {
  if (!BAK) return;
  step("saveBak", function () {
    var f = new File(BAK_FILE, "w");
    f.write(BAK.dev + "\n" + BAK.tok + "\n");
    f.flush();
    f.close();
    return true;
  });
}

function loadBak() {
  if (BAK) return;
  var ok = step("bakExists", function () {
    return Java.use("java.io.File").$new(J.String.$new(BAK_FILE)).exists();
  });
  if (!ok) return;
  var txt = step("loadBak", function () {
    var RAF = Java.use("java.io.RandomAccessFile");
    var f = RAF.$new(J.String.$new(BAK_FILE), "r");
    var n = parseInt(String(f.length()));
    if (n <= 0 || n > 4096) { f.close(); return null; }
    var buf = Java.array("byte", new Array(n).fill(0));
    f.readFully(buf);
    f.close();
    return J.String.$new(buf, "UTF-8").toString();
  });
  if (!txt) return;
  var parts = String(txt).split("\n");
  if (parts.length >= 2 && parts[1].indexOf("eyJ") === 0 && parts[0].length === 40) {
    BAK = { tok: parts[1], dev: parts[0] };
    log("读回备用游客 sub=" + uidOf(BAK.tok));
  }
}

/* 账号的 freeWatches（50 次）用完后服务端就不再给 1 前缀 key；
 * POST /api/user/traveler 会开一个带全新额度的游客，aut 只需是任意有效 token。 */

function readText(path) {
  return step("readText", function () {
    var JFile = Java.use("java.io.File");
    if (!JFile.$new(J.String.$new(path)).exists()) return null;
    var RAF = Java.use("java.io.RandomAccessFile");
    var f = RAF.$new(J.String.$new(path), "r");
    var n = parseInt(String(f.length()));
    if (n <= 0 || n > 4096) { f.close(); return null; }
    var buf = Java.array("byte", new Array(n).fill(0));
    f.readFully(buf);
    f.close();
    return J.String.$new(buf, "UTF-8").toString();
  });
}

function writeText(path, s) {
  step("writeText", function () {
    var f = new File(path, "w");
    f.write(s);
    f.flush();
    f.close();
    return true;
  });
}

function recentMints() {
  var txt = readText(MINT_FILE);
  if (!txt) return [];
  var cut = Date.now() / 1000 - MINT_WINDOW;
  return String(txt).split("\n").filter(function (l) { return parseFloat(l) > cut; });
}

function noteMint() {
  var keep = recentMints();
  keep.push(String(Date.now() / 1000));
  writeText(MINT_FILE, keep.join("\n") + "\n");
}

function mintTraveler() {
  var recent = recentMints();
  if (recent.length >= MINT_WINDOW_MAX) {
    log("换号节流：" + (MINT_WINDOW / 60) + " 分钟内已换 " + recent.length + " 个，等下一轮");
    return null;
  }
  var bases = [];
  if (BAK) bases.push(BAK.tok);
  if (TOKEN && bases.indexOf(TOKEN) < 0) bases.push(TOKEN);
  if (!bases.length) { log("mint 缺有效 aut，跳过"); return null; }
  for (var i = 0; i < bases.length; i++) {
    var signer = { tok: bases[i], dev: newDev(), app: bases[i] === TOKEN };
    var sent = signer.tok;
    var r = apiReq(signer, "POST", "/api/user/traveler", "{}");
    if (!r || r.code !== 200) { log("mint 失败 HTTP " + (r ? r.code : "null")); continue; }
    var inner = readInner(r, sent, signer, "mint");
    if (inner === null || !inner.token) { log("mint 响应里没有 token"); continue; }
    BAK = { tok: String(inner.token), dev: signer.dev };
    stats.minted++;
    noteMint();
    saveBak();
    log("已开新游客 sub=" + uidOf(BAK.tok) + " 额度=" + inner.remainingWatches + "/" + inner.freeWatches);
    return BAK;
  }
  return null;
}

function signFor(signer, vid) {
  var sent = signer.tok;
  var r = apiReq(signer, "GET", "/api/video/getVideoById?videoId=" + vid);
  if (!r || r.code !== 200) { log("签 " + vid + " 失败 HTTP " + (r ? r.code : "null")); return null; }
  var inner = readInner(r, sent, signer, "vid" + vid);
  return inner === null ? null : (inner.authKey || null);
}

function trySigners() {
  var list = [];
  if (BAK) list.push({ s: BAK, n: "游客", uid: function () { return uidOf(BAK.tok); } });
  if (TOKEN) list.push({ s: { tok: TOKEN, dev: DEV_ID, app: true }, n: "app", uid: function () { return uidOf(TOKEN); } });
  for (var i = 0; i < list.length; i++) {
    for (var j = 0; j < FREE_IDS.length; j++) {
      var ak = signFor(list[i].s, FREE_IDS[j]);
      if (!ak) continue;
      if (noteKey(ak)) {
        stats.signed++;
        log("已现签 1 前缀 key（" + list[i].n + " sub=" + list[i].uid() + "）" +
            ak.substring(0, 10) + " … " + ak.substring(43, 52));
        return true;
      }
      log("免费片 " + FREE_IDS[j] + " 对 " + list[i].n + " 号（sub=" + list[i].uid() +
          "）只给 0 前缀，配额已尽");
    }
  }
  return false;
}

function fetchSignedKey() {
  var tid = Process.getCurrentThreadId();
  if (fetching) { log("补签已在进行中，跳过 tid=" + tid); return; }
  if (!ensureJava()) return;
  fetching = true;
  log("补签开始 tid=" + tid + " token=" + (TOKEN ? "有" : "无") + " bak=" + (BAK ? "有" : "无"));
  try {
    Java.perform(function () {
      loadBak();
      if (trySigners()) return;
      if (mintTraveler() && trySigners()) return;
      log("补签未拿到 1 前缀 key");
    });
  } catch (e) {
    log("catch 命中 tid=" + tid + " err=" + errStr(e));
  } finally {
    log("补签结束 tid=" + tid + " goodKey=" + (goodKey ? "yes" : "no"));
    fetching = false;
  }
}

function scheduleFetch(delay) {
  setTimeout(function () { fetchSignedKey(); }, delay || 0);
}

/* ---------- 安装 hook ---------- */

function install(base) {
  // 这些轮换域名会随版本变化，从 app 自己拼出的 authPath 里学当前 API host
  Interceptor.attach(base.add(0x641310), {
    onEnter: function () { init(this.context); },
    onLeave: function (retval) {
      try {
        var s = readDartStr(retval);
        if (!s) return;
        var m = /^(https?:\/\/[^/]+)\/api\/m3u8\/decode\/authPath/.exec(s[2]);
        if (m && m[1] !== API_HOST) {
          API_HOST = m[1];
          log("API host 自动校正为 " + API_HOST);
        }
      } catch (e) {}
    }
  });

  Interceptor.attach(base.add(OFF_READ), {
    onEnter: function () { init(this.context); },
    onLeave: function (retval) {
      try {
        if (retval.isNull()) return;
        var s = readDartStr(retval);
        if (!s) return;
        var v = s[2];
        if (v.length > 100 && v.length < 400 && v.indexOf("eyJ") === 0) {
          if (v !== TOKEN) {
            TOKEN = v;
            stats.tokens++;
            log("抓到 token（sub=" + v.split(".")[1].substring(0, 6) + "…）");
            scheduleFetch(1500);
          }
        }
      } catch (e) {}
    }
  });

  Interceptor.attach(base.add(OFF_ASSTRING), {
    onEnter: function () {
      init(this.context);
      var kv = readDartStr(this.context.x2);
      this.k = kv ? kv[2] : null;
    },
    onLeave: function (retval) {
      if (this.k !== "authKey" || retval.isNull()) return;
      var v = readDartStr(retval);
      if (!v) return;
      var s = v[2];
      if (!KEY_RE.test(s)) return;

      if (flagOf(s) === "1") {           // 本来就有权限，顺手缓存
        noteKey(s);
        return;
      }
      if (keyAlive() && goodKey.length === s.length) {
        writeStr(v, goodKey);
        stats.swaps++;
        log("[替换] 12秒 key -> 正片 key（" + s.substring(43, 52) + " -> " +
            goodKey.substring(43, 52) + "）第 " + stats.swaps + " 次，key 已 " +
            Math.round(Date.now() / 1000 - goodKeyTs) + "s");
      } else {
        log("遇到 0 前缀 key 但还没有可用 1 前缀 key，立即补签");
        scheduleFetch(0);
      }
    }
  });
  log("hook 已安装 base=" + base);
}

/* ---------- 启动 ---------- */

log("gadget 脚本启动 pid=" + Process.id + " frida=" + Frida.version);
ensureJava();

(function waitLibapp(tries) {
  var m = Process.findModuleByName("libapp.so");
  if (m) {
    install(m.base);
    scheduleFetch(4000);
    setInterval(function () {
      if (!TOKEN && !BAK) { log("定时续期：还没有可用 token，跳过"); return; }
      log("定时续期开始");
      fetchSignedKey();
    }, REFRESH_MS);
    return;
  }
  if (tries < 150) setTimeout(function () { waitLibapp(tries + 1); }, 100);
  else log("等不到 libapp.so，放弃");
})(0);
