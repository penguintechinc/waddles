# webui-sweep

Visits every hub-webui route (enumerated from `admin/hub_module/frontend/src/App.jsx`) after a real form login and
reports console errors, pageerrors, HTTP >= 400 / failed requests and error-state text per page.
Findings tracked by #1462 are tagged `KNOWN-PENDING-#1462`. Results: `results/{results.json,results.md}` (ignored);
screenshots of non-clean pages: `/tmp/playwright-waddles`.

```bash
export WEBUI_ADMIN_EMAIL=...; read -rs WEBUI_ADMIN_PASSWORD; export WEBUI_ADMIN_PASSWORD
make webui-sweep BASE_URL=https://waddles-alpha.penguintech.cloud:8443
make webui-sweep BASE_URL=https://<lb-ip>:8443 HOST_HEADER=waddles-alpha.penguintech.cloud   # internal LB + Host
# port-forward mode (webui has no /api proxy, so the sweep starts one; localhost only):
WEBUI_UPSTREAM=https://localhost:18080 API_UPSTREAM=https://localhost:18204 make webui-sweep
```

Exit: 0 clean/known only, 1 failures, 2 setup error or zero routes visited.
