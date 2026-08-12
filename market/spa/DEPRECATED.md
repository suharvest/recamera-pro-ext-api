# ⚠️ DEPRECATED — 历史产物，勿再作为部署前端

**状态（2026-08-12）**：本目录的 vanilla SPA（`index.html`）是 App Center 前端的**早期历史实现**，
现已**不再是现役前端**。

## 现役前端

App Center 的现役前端是**官方 web 原生 React 页 `/app-center`**
（`project/app/recamera_web/recamera_web_react/`，Sidebar tab + 11 组件），
后端仍对接本仓库的 `appmgr` `/api/appMgr`（:8130）。

## 本目录的定位

- `index.html` / `apps/` / `THEME.md`：**仅留作参考**（历史实现、交互/主题参考），
  **不要**再把它作为部署前端打包或挂到 `/appcenter/`。
- 相关设计文档见 `docs/guide/app-center-publishing.md`（原 `APP_CENTER_PORT_DESIGN.md`）。
- appmgr **后端 / 打包 / catalog 仍然有效**，参见 `docs/guide/app-center-publishing.md` 与 `docs/guide/kit-design.md`。

## 为什么不删

保留历史实现以便回溯交互设计与主题（`THEME.md`）细节；删除会丢失参考价值。
如需清理，请确认现役 React 页已覆盖全部功能后再评估。
