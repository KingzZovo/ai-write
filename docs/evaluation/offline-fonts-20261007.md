# 前端字体构建依赖修复

原布局在构建阶段通过 next/font/google 下载 Geist 和 Geist Mono，外部字体服务不可达会阻止发布。

已移除布局的字体下载与注入，globals.css 直接定义系统无衬线及等宽字体栈，含苹方、微软雅黑和 Noto Sans CJK SC 回退。编辑器原有衬线字体设置保留。系统字体会随设备变化，不保证字宽与 Geist 完全相同。

验证：
- `npm run build -- --webpack` 完成生产编译、TypeScript、20 个静态页面生成与构建追踪，退出 0。
- `npx tsc --noEmit` 通过。
- 生成 CSS 含 system-ui / ui-monospace，无 --font-geist 悬空变量或 Google 字体链接。
- 默认 `npm run build` 的 Turbopack 在处理 CSS 时因沙箱禁止绑定端口失败；未修改默认构建器，不能将此路径写成已通过。
- `git diff --check` 通过；这一字体配置变更不新增镜像实现的单元测试。

按 shadow-mind 的无只读隔离回退，在代码冻结后本地复核字体变量引用、编辑器字体与发布声明，未发现额外问题。真实浏览器的跨平台排版、移动端换行和发布尚未验证。此批不涉及模型路由或小说质量结论。
