// Mac 侧「落点捕获」探针 v2（轨迹采样版，诊断用）。
// 目的：定性判断 Deskflow 从 Windows 拖文件跨屏到 Mac 时，
//       Mac 后台进程能否观测到「左键按下状态」与「松开位置」。
// 采样：20ms 一次；只要「按键状态变化」或「位置变化超过 6px」或「每 1s」就输出一行，
//       每行必带 btn（0/1），便于还原轨迹到底是 btn=1（按键被转发）还是 btn=0（未被转发）。
// 坐标：NSEvent.mouseLocation = AppKit 屏幕坐标（原点左下）。

ObjC.import('AppKit');
ObjC.import('Foundation');

var INTERVAL = 0.02;
var POS_STEP = 6;     // 位置量化步长（px）
var HB_EVERY = 1.0;

var STDOUT = $.NSFileHandle.fileHandleWithStandardOutput;
function emit(o) {
  var s = $.NSString.alloc.initWithUTF8String(JSON.stringify(o) + '\n');
  STDOUT.writeData(s.dataUsingEncoding($.NSUTF8StringEncoding));
}
function now() { return Number($.NSDate.date.timeIntervalSince1970); }
function loc() {
  try {
    var p = $.NSEvent.mouseLocation;
    if (typeof p === 'function') p = p();
    return { x: Math.round(Number(p.x)), y: Math.round(Number(p.y)) };
  } catch (e) { return { x: null, y: null }; }
}
function btn() {
  try {
    var v = $.NSEvent.pressedMouseButtons;
    if (typeof v === 'function') v = v();
    return Number(v) & 1;
  } catch (e) { return -1; }
}

var lastBtn = btn();
var L = loc();
var lq = [Math.floor(L.x / POS_STEP), Math.floor(L.y / POS_STEP)];
var lastEmit = now();
emit({ event: 'start', pid: Number($.NSProcessInfo.processInfo.processIdentifier),
       btn: lastBtn, x: L.x, y: L.y, interval: INTERVAL });

while (true) {
  $.NSThread.sleepForTimeInterval(INTERVAL);
  try {
    var b = btn();
    var P = loc();
    var q = [Math.floor(P.x / POS_STEP), Math.floor(P.y / POS_STEP)];
    var moved = (q[0] !== lq[0] || q[1] !== lq[1]);
    var bchg = (b !== lastBtn);
    var due = (now() - lastEmit >= HB_EVERY);
    if (!moved && !bchg && !due) continue;
    var ev = 'pos';
    if (bchg) ev = (b === 1 ? 'down' : 'up');
    else if (due && !moved) ev = 'hb';
    emit({ event: ev, t: now(), btn: b, x: P.x, y: P.y });
    lastBtn = b; lq = q; lastEmit = now();
  } catch (e) {
    emit({ event: 'error', err: String(e) });
  }
}
