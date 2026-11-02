# Windows 侧（未实现）

这个目录是占位。目前只有设计，没有可直接运行的代码——原因见 `../docs/03-windows-port.md`。

## 缺什么

要补齐四层，只有一层是纯体力活：

| 层 | 需要的东西 | 现状 |
|---|---|---|
| 虚拟屏 | 一个已签名的 IddCx 虚拟显示驱动 | 建议直接复用 Parsec VDD / spacedesk，不自研 |
| 窗口 | `SetWindowPos` + `DwmGetWindowAttribute` | 两小时能写完 |
| 光标 | `ShowCursor` + `SetWindowsHookEx(WH_MOUSE_LL)` | 需要小心计数配平 |
| 注入 | WinRT `GattServiceProvider` 发布 HOGP | 取决于蓝牙适配器是否支持外设角色 |
| 采集 | **无开源等价物** | 见文档，建议先用"盲控"方案 |

## 建议的实现顺序

1. 装第三方虚拟显示驱动，验证光标能移进第二块屏；
2. 写一个最小钩子程序，验证能吞掉该屏上的鼠标事件并算出相对位移；
3. 用 WinRT 发布只含鼠标 Report 的 HOGP 服务，验证 iPhone 能配对并显示指针；
4. 三步都通了，再考虑把它做成托盘常驻程序。

前两步不依赖 iPhone，可以先用"把事件打印到控制台"验证；第三步不通就说明适配器不支持外设角色，换 USB 蓝牙适配器（CSR8510 一类支持双角色的）再试。

## 接口边界

如果要让 Windows 版和 macOS 版共用一套上层逻辑，建议先把这个接口定下来：

```c
// 与平台无关的"一块手机屏幕"抽象
typedef struct {
    int      (*open)(void);                       // 建立与手机的连接
    void     (*close)(void);
    int      (*screen_rect)(int *x, int *y, int *w, int *h);  // 朝外暴露画面区
    void     (*hide_cursor)(int hidden);
    int      (*send_relative)(int dx, int dy);    // 像素位移
    int      (*send_key)(uint16_t usage, int down);
} ios_screen_backend;

// 平台侧只提供三件事
ios_screen_backend *platform_backend(void);        // 造/拿虚拟屏 + 注入
```

macOS 的 `psagent.m` 里那三层逻辑（几何闭环、光标配平、焦点策略）可以原样搬到这套接口之上，`docs/01-architecture.md` 的状态机也直接适用。
