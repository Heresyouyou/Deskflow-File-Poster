-- Finder 落点查询：给定屏幕坐标（AX 系：原点左上、y 向下），
-- 返回包含该点的 Finder 窗口 target 目录 POSIX 路径；无命中返回空串。
-- 注意：必须用「索引循环 + window i」，`repeat with w in windows` 在 Finder 下会取错。
on run argv
	set fx to (item 1 of argv) as integer
	set fy to (item 2 of argv) as integer
	tell application "Finder"
		set n to count of windows
		repeat with i from 1 to n
			try
				set w to window i
				set {l, t, r, b} to bounds of w
				if fx is greater than or equal to l and fx is less than or equal to r and fy is greater than or equal to t and fy is less than or equal to b then
					set tg to ""
					try
						set tg to POSIX path of (target of w as alias)
					end try
					return tg
				end if
			end try
		end repeat
	end tell
	return ""
end run
