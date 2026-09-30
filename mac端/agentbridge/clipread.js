// 读当前剪贴板里的文件列表，输出一行 JSON：{"cc":N,"paths":[...]}
// 用途：inboxwatch 写剪贴板后的核验；也可人工排障。

ObjC.import('AppKit');
ObjC.import('Foundation');

function run() {
  var pb = $.NSPasteboard.generalPasteboard;
  var out = { cc: Number(pb.changeCount), paths: [] };
  try {
    var a = pb.readObjectsForClassesOptions($.NSArray.arrayWithObject($.NSURL), $());
    if (a && !a.isNil()) {
      for (var i = 0; i < a.count; i++) out.paths.push(ObjC.unwrap(a.objectAtIndex(i).path));
    }
  } catch (e) {}
  return JSON.stringify(out);
}
