// Mac 侧跨屏拖拽源端守护（JXA，常驻）。轮询 NSDragPboard.changeCount，
// 只在变化时向 stdout 打一行 JSON。已实测：后台进程可观测 NSDragPboard，
// 且拖拽文件时能直接读到被拖路径（NSFilenamesPboardType / public.file-url）。
// 输出事件：
//   {"event":"start","cc":N,"pid":N}
//   {"event":"drag","cc":N,"paths":[...],"ptr":{x,y}|null,"screen":{w,h}|null}
//   {"event":"clear","cc":N}                 拖拽板变了但无文件载荷（如拖文本）
//   {"event":"error","err":"..."}
// 注意：本机拖拽“结束”不会使 NSDragPboard 再次变化（内容会保留），
// 因此“拖拽结束/取消”不靠本守护判断，由目标端 drag_drop 回执 + 源端 TTL 兜底。

ObjC.import('AppKit');
ObjC.import('Foundation');

var INTERVAL = 0.05;   // 50 ms：拖拽开始要尽量早被捕捉，纯本机轮询零网络成本

var STDOUT = $.NSFileHandle.fileHandleWithStandardOutput;
function emit(obj) {
  var s = $.NSString.alloc.initWithUTF8String(JSON.stringify(obj) + '\n');
  STDOUT.writeData(s.dataUsingEncoding($.NSUTF8StringEncoding));
}

function dragPasteboard() {
  return $.NSPasteboard.pasteboardWithName($.NSDragPboard);
}

function filePaths(pb) {
  var res = [];
  try {
    var f = pb.propertyListForType($.NSFilenamesPboardType);
    if (f && !f.isNil()) {
      var arr = ObjC.deepUnwrap(f);
      if (arr && arr.length) {
        for (var i = 0; i < arr.length; i++) {
          var p = String(arr[i]);
          if (p && res.indexOf(p) < 0) res.push(p);
        }
        return res;
      }
    }
  } catch (e) {}
  try {
    var o = pb.readObjectsForClassesOptions($.NSArray.arrayWithObject($.NSURL), $());
    if (o && !o.isNil()) {
      for (var j = 0; j < o.count; j++) {
        var q = ObjC.unwrap(o.objectAtIndex(j).path);
        if (q && res.indexOf(q) < 0) res.push(q);
      }
    }
  } catch (e) {}
  return res;
}

// 拖拽开始瞬间的指针位置 + 主屏尺寸（目标端做坐标换算用；取不到给 null）
function ptrScreen() {
  var ptr = null, screen = null;
  try {
    var ml = $.NSEvent.mouseLocation;
    ptr = { x: Number(ml.x), y: Number(ml.y) };
  } catch (e) { ptr = null; }
  try {
    var fr = $.NSScreen.mainScreen.frame;
    screen = { w: Number(fr.size.width), h: Number(fr.size.height) };
  } catch (e) { screen = null; }
  return { ptr: ptr, screen: screen };
}

var last = Number(dragPasteboard().changeCount);
emit({ event: 'start', cc: last, pid: Number($.NSProcessInfo.processInfo.processIdentifier) });

while (true) {
  $.NSThread.sleepForTimeInterval(INTERVAL);
  try {
    var pb = dragPasteboard();
    var cc = Number(pb.changeCount);
    if (cc === last) continue;
    last = cc;
    var paths = filePaths(pb);
    if (paths.length > 0) {
      var ps = ptrScreen();
      emit({ event: 'drag', cc: cc, paths: paths, ptr: ps.ptr, screen: ps.screen });
    } else {
      emit({ event: 'clear', cc: cc });
    }
  } catch (e) {
    emit({ event: 'error', err: String(e) });
  }
}
