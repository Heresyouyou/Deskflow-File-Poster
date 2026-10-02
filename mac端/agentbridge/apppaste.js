// 阶段 B：把 inbox 文件投递到非 Finder 的 App（微信 / QQ 等）。
// 方式：写剪贴板 furl（记账防回环）→ 激活目标 App → 合成 ⌘V。
// 调用：osascript -l JavaScript apppaste.js '{"paths":["/a.txt"],"pid":123,"bundle":"com.tencent.xinWeChat"}'
// 输出：{"ok":true,"count":1,"cc":108,"activated":"com.tencent.xinWeChat","pasted":true}
// 失败会带 err（如 clipboard-write-failed / not-frontmost / no-target-app），且不误粘到别的 App。

ObjC.import('AppKit');
ObjC.import('Foundation');
ObjC.import('CoreGraphics');

var K_V = 9;             // kVK_ANSI_V
var CMD = 0x100000;      // kCGEventFlagMaskCommand
var HID = 0;             // kCGHIDEventTap
var ACTIVATE_IGNORE = 2; // NSApplicationActivateIgnoringOtherApps

function writeState(o) {
  var p = ObjC.unwrap($.NSHomeDirectory()) + '/AgentBridge/.clip_self.json';
  var s = $.NSString.alloc.initWithUTF8String(JSON.stringify(o));
  s.writeToFileAtomicallyEncodingError(p, true, $.NSUTF8StringEncoding, $());
}

function str(v) {
  if (v === null || v === undefined) return null;
  try { var u = ObjC.unwrap(v); return typeof u === 'string' ? u : String(u); }
  catch (e) { return null; }
}

function frontBundle() {
  try {
    var f = $.NSWorkspace.sharedWorkspace.frontmostApplication;
    if (!f || f.isNil()) return null;
    return str(f.bundleIdentifier);
  } catch (e) { return null; }
}

function targetApp(pid, bundle) {
  var app = null;
  if (pid) {
    try {
      app = $.NSRunningApplication.runningApplicationWithProcessIdentifier(pid);
      if (app && app.isNil()) app = null;
    } catch (e) {}
  }
  if (!app && bundle) {
    try {
      var arr = $.NSRunningApplication.runningApplicationsWithBundleIdentifier($(bundle));
      if (arr && !arr.isNil() && arr.count > 0) app = arr.objectAtIndex(0);
    } catch (e) {}
  }
  return app;
}

function activate(app) {
  try {
    if (typeof app.activate === 'function' && app.activate()) return true;   // macOS 14+
  } catch (e) {}
  try { return !!app.activateWithOptions(ACTIVATE_IGNORE); } catch (e) { return false; }
}

function postKey(keycode, down) {
  var e = $.CGEventCreateKeyboardEvent($(), keycode, down);
  $.CGEventSetFlags(e, CMD);
  $.CGEventPost(HID, e);
}

function run(argv) {
  var req = JSON.parse(argv[0]);
  var paths = req.paths || [];
  var out = { ok: false, count: paths.length };
  try {
    if (!paths.length) { out.err = 'no-paths'; return JSON.stringify(out); }

    var app = targetApp(req.pid, req.bundle);
    var bundle = req.bundle || null;
    if (app) {
      var b = str(app.bundleIdentifier);
      if (b) bundle = b;
      out.activated = bundle;
      activate(app);
    }
    if (!bundle) { out.err = 'no-target-app'; return JSON.stringify(out); }

    // ① 写 furl 到剪贴板（记账，防 clipwatch 把它回灌 Windows）
    var pb = $.NSPasteboard.generalPasteboard;
    var before = Number(pb.changeCount);
    writeState({ pending: true, before: before, src: 'apppaste' });
    var arr = $.NSMutableArray.array;
    for (var i = 0; i < paths.length; i++) arr.addObject($.NSURL.fileURLWithPath(paths[i]));
    pb.clearContents;
    var wrote = pb.writeObjects(arr);
    var cc = Number(pb.changeCount);
    writeState({ pending: false, before: before, cc: cc, src: 'apppaste' });
    out.cc = cc;
    if (!wrote) { out.err = 'clipboard-write-failed'; return JSON.stringify(out); }

    // ② 等目标 App 到前台（最多 ~2.4s）；没到前台就绝不合成 ⌘V，避免误粘
    var tries = 0;
    while (frontBundle() !== bundle && tries < 12) {
      if (tries === 4 || tries === 8) activate(app);
      $.NSThread.sleepForTimeInterval(0.2);
      tries++;
    }
    out.frontmost = frontBundle();
    if (out.frontmost !== bundle) { out.err = 'not-frontmost'; return JSON.stringify(out); }

    // ③ 合成 ⌘V
    postKey(K_V, true);
    $.NSThread.sleepForTimeInterval(0.05);
    postKey(K_V, false);
    out.pasted = true;
    out.ok = true;
  } catch (e) {
    out.err = String(e);
  }
  return JSON.stringify(out);
}
