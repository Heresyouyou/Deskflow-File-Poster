// Mac 侧剪贴板守护（JXA，常驻）。内部轮询 changeCount，只在变化时向 stdout 打一行 JSON。
// 输出事件：
//   {"event":"start","cc":N,"pid":N}
//   {"event":"files","cc":N,"paths":[...]}   剪贴板里是文件列表
//   {"event":"image","cc":N,"path":"..."}    剪贴板是图片，已落成 PNG 临时文件
//   {"event":"text","cc":N,"path":"...","len":N} 剪贴板是文本，已落成 UTF-8 临时文件
//   {"event":"nonfile","cc":N}               剪贴板变了但不是文件/图片/文本
//   {"event":"self","cc":N}                  自己写入的，忽略
//   {"event":"error","err":"..."}
// 防回环：clipwrite.js 写剪贴板前后把 {pending,before,cc} 记到 .clip_self.json。

ObjC.import('AppKit');
ObjC.import('Foundation');

var HOME = ObjC.unwrap($.NSHomeDirectory());
var STATE = HOME + '/AgentBridge/.clip_self.json';
var INTERVAL = 0.25;   // 与 Windows 侧对齐：0.25 s 纯本机轮询，零网络成本

var STDOUT = $.NSFileHandle.fileHandleWithStandardOutput;
function emit(obj) {
  var s = $.NSString.alloc.initWithUTF8String(JSON.stringify(obj) + '\n');
  STDOUT.writeData(s.dataUsingEncoding($.NSUTF8StringEncoding));
}

function readState() {
  try {
    var d = $.NSFileManager.defaultManager.contentsAtPath(STATE);
    if (!d || d.isNil()) return null;
    var s = $.NSString.alloc.initWithDataEncoding(d, $.NSUTF8StringEncoding);
    return JSON.parse(ObjC.unwrap(s));
  } catch (e) { return null; }
}

function writeState(o) {
  try {
    var s = $.NSString.alloc.initWithUTF8String(JSON.stringify(o));
    s.writeToFileAtomicallyEncodingError(STATE, true, $.NSUTF8StringEncoding, $());
  } catch (e) {}
}

function filePaths(pb) {
  var res = [];
  try {
    var arr = pb.readObjectsForClassesOptions($.NSArray.arrayWithObject($.NSURL), $());
    if (!arr || arr.isNil()) return res;
    for (var i = 0; i < arr.count; i++) {
      var p = ObjC.unwrap(arr.objectAtIndex(i).path);
      if (p && res.indexOf(p) < 0) res.push(p);
    }
  } catch (e) {}
  return res;
}

// 交给我们之前先落成临时文件，避免长 base64 走 stdout。
// 顺序按协议 §3 的优先级：files > image > text。
function dumpImage(pb, cc) {
  var d = null;
  try { d = pb.dataForType('public.png'); } catch (e) {}
  if (!d || d.isNil() || Number(d.length) === 0) {
    d = null;
    var t = null;
    try { t = pb.dataForType('public.tiff'); } catch (e) {}
    if (t && !t.isNil() && Number(t.length) > 0) {
      try {
        var img = $.NSImage.alloc.initWithData(t);
        var rep = (img && !img.isNil()) ? $.NSBitmapImageRep.imageRepWithData(img.TIFFRepresentation) : null;
        var png = (rep && !rep.isNil())
          ? rep.representationUsingTypeProperties($.NSBitmapImageFileTypePNG, $()) : null;
        if (png && !png.isNil()) d = png;
      } catch (e) {}
    }
  }
  if (!d || d.isNil() || Number(d.length) === 0) return null;
  var p = '/tmp/ab-clip-img-' + cc + '.png';
  try { return d.writeToFileAtomically($(p), true) ? p : null; } catch (e) { return null; }
}

function dumpText(pb, cc) {
  var t = null;
  try {
    var s = pb.stringForType('public.utf8-plain-text');
    if (s && !s.isNil()) t = ObjC.unwrap(s);
  } catch (e) { return null; }
  if (typeof t !== 'string' || t.length === 0) return null;
  var p = '/tmp/ab-clip-txt-' + cc + '.txt';
  try {
    var str = $.NSString.alloc.initWithUTF8String(t);
    if (!str.writeToFileAtomicallyEncodingError($(p), true, $.NSUTF8StringEncoding, $())) return null;
  } catch (e) { return null; }
  return { path: p, len: t.length };
}

var pb = $.NSPasteboard.generalPasteboard;
var last = Number(pb.changeCount);
emit({ event: 'start', cc: last, pid: Number($.NSProcessInfo.processInfo.processIdentifier) });

while (true) {
  $.NSThread.sleepForTimeInterval(INTERVAL);
  try {
    var cc = Number(pb.changeCount);
    if (cc === last) continue;
    last = cc;

    var st = readState();
    if (st) {
      if (st.pending && cc > Number(st.before)) {
        writeState({ pending: false, cc: cc, before: Number(st.before), src: 'clipwatch' });
        emit({ event: 'self', cc: cc });
        continue;
      }
      if (Number(st.cc) === cc) { emit({ event: 'self', cc: cc }); continue; }
    }

    var paths = filePaths(pb);
    if (paths.length > 0) { emit({ event: 'files', cc: cc, paths: paths }); continue; }

    var img = dumpImage(pb, cc);
    if (img) { emit({ event: 'image', cc: cc, path: img }); continue; }

    var txt = dumpText(pb, cc);
    if (txt) { emit({ event: 'text', cc: cc, path: txt.path, len: txt.len }); continue; }

    emit({ event: 'nonfile', cc: cc });
  } catch (e) {
    emit({ event: 'error', err: String(e) });
  }
}
