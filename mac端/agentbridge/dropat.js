// Mac 侧落点查询（JXA，单次执行）。供 dropwatch.py 在收到 Windows drop 触发时调用。
// 主路径：CGWindowListCopyWindowInfo（窗口服务器快照，屏幕上前→后排序）取光标所在窗口的归属 App。
//   相比 AXUIElementCopyElementAtPosition：不依赖目标 App 响应 AX，不会出现
//   -25208 (NotImplemented，常见于微信等非原生 App) / -25211 (APIDisabled) 这类偶发失败。
// 兜底：CGWindowList 拿不到时退回 AX hit-test。
// 注意：JXA 里 $.kCGWindow*/$.kCGWindowListOption* 这些常量取不到（是 Ref），
//       字典键必须用**字面字符串** 'kCGWindowLayer' 等；option 位用字面整数。
// 输出一行 JSON：
//   {"ok":true,"cursor":{"ax":X,"ay":Y},"hit":{"ax":X,"ay":Y},
//    "pid":123,"app":"访达","bundle":"com.apple.finder","win":"...","method":"cgwindowlist"}
// 坐标：NSEvent.mouseLocation 原点左下 → 换算成「原点左上」的全局坐标（主屏高 - y）。

ObjC.import('AppKit');
ObjC.import('Foundation');
ObjC.import('CoreGraphics');
ObjC.import('ApplicationServices');

var OPT_ONSCREEN = 1;          // kCGWindowListOptionOnScreenOnly
var OPT_EXCL_DESKTOP = 16;     // kCGWindowListExcludeDesktopElements

function emit(o) {
  var s = $.NSString.alloc.initWithUTF8String(JSON.stringify(o) + '\n');
  $.NSFileHandle.fileHandleWithStandardOutput.writeData(s.dataUsingEncoding($.NSUTF8StringEncoding));
}
function str(v) {
  if (v === null || v === undefined) return null;
  try { var u = ObjC.unwrap(v); return (u === null || u === undefined) ? null : (typeof u === 'string' ? u : String(u)); }
  catch (e) { return null; }
}
function num(v) {
  if (v === null || v === undefined) return null;
  try { var u = ObjC.unwrap(v); var n = Number(u); return isNaN(n) ? null : n; }
  catch (e) { try { var n2 = Number(v); return isNaN(n2) ? null : n2; } catch (e2) { return null; } }
}
function dget(d, k) { try { return num(d.objectForKey(k)); } catch (e) { return null; } }

function viaWindowList(fx, fy) {
  var arr = ObjC.castRefToObject($.CGWindowListCopyWindowInfo(OPT_ONSCREEN | OPT_EXCL_DESKTOP, 0));
  if (!arr) return null;
  var n = Number(arr.count);
  for (var i = 0; i < n; i++) {
    var w = arr.objectAtIndex(i);
    if (num(w.objectForKey('kCGWindowLayer')) !== 0) continue;   // 只看普通 App 窗口层
    var b = w.objectForKey('kCGWindowBounds');
    if (!b) continue;
    var x = dget(b, 'X'), y = dget(b, 'Y'), wd = dget(b, 'Width'), ht = dget(b, 'Height');
    if (x === null || y === null || !wd || !ht) continue;
    if (wd < 40 || ht < 40) continue;                            // 跳过小浮动件
    if (fx >= x && fx < x + wd && fy >= y && fy < y + ht) {
      var pid = num(w.objectForKey('kCGWindowOwnerPID'));
      var own = str(w.objectForKey('kCGWindowOwnerName'));
      var title = str(w.objectForKey('kCGWindowName'));
      var out = { pid: pid, owner: own, win: title,
                  bounds: { x: x, y: y, w: wd, h: ht }, method: 'cgwindowlist' };
      if (pid) {
        var a = $.NSRunningApplication.runningApplicationWithProcessIdentifier(pid);
        if (a && !a.isNil()) {
          out.app = str(a.localizedName) || own;
          out.bundle = str(a.bundleIdentifier);
        }
      }
      if (!out.app) out.app = own;
      return out;
    }
  }
  return null;
}

// 兜底：AX hit-test
function viaAX(fx, fy) {
  try {
    var sys = $.AXUIElementCreateSystemWide();
    var elRef = Ref();
    if ($.AXUIElementCopyElementAtPosition(sys, fx, fy, elRef) !== 0 || !elRef[0]) return null;
    var el = elRef[0], p = Ref();
    if ($.AXUIElementGetPid(el, p) !== 0) return null;
    var out = { pid: num(p[0]), method: 'ax' };
    var a = $.NSRunningApplication.runningApplicationWithProcessIdentifier(p[0]);
    if (a && !a.isNil()) { out.app = str(a.localizedName); out.bundle = str(a.bundleIdentifier); }
    return out;
  } catch (e) { return null; }
}

function main() {
  var fr = $.NSScreen.mainScreen.frame;
  var H = Number(fr.size.height);
  var ml = $.NSEvent.mouseLocation;
  var ax = Number(ml.x), ay = Number(ml.y);
  var fx = Math.round(ax), fy = Math.round(H - ay);
  var res = { ok: false, cursor: { ax: Math.round(ax), ay: Math.round(ay) }, hit: { ax: fx, ay: fy } };
  try {
    var hit = viaWindowList(fx, fy);
    if (!hit) hit = viaAX(fx, fy);
    if (hit) {
      res.pid = hit.pid; res.app = hit.app; res.bundle = hit.bundle;
      res.win = hit.win; res.bounds = hit.bounds; res.method = hit.method;
      res.ok = !!hit.pid;
    } else {
      res.why = 'no-window-at-point';
    }
  } catch (err) { res.error = String(err); }
  emit(res);
}
main();
