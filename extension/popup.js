// ============================================================
// 扩展弹窗：只保存本机 API 地址，向 background 发采集指令。
// 不再使用采集令牌；清空操作必须二次确认。
// ============================================================
const apiInput = document.getElementById("api");
const siteInput = document.getElementById("site");
const statusBox = document.getElementById("status");
const captureBtn = document.getElementById("capture");
const saveBtn = document.getElementById("save");
const clearBtn = document.getElementById("clear");
const autoDetailInput = document.getElementById("autoDetail");

// 从 chrome.storage.local 读取设置；扩展只在本机保存，不云端同步。
chrome.storage.local.get(["apiUrl", "siteUrl", "autoDetail"], (data) => {
  if (data.apiUrl) apiInput.value = data.apiUrl;
  if (data.siteUrl) siteInput.value = data.siteUrl;
  autoDetailInput.checked = Boolean(data.autoDetail);
});

function normalizedApi() {
  return apiInput.value.trim().replace(/\/+$/, "");
}

function validateApi(apiUrl) {
  if (!/^https?:\/\/(127\.0\.0\.1|localhost)(:\d+)?$/.test(apiUrl)) {
    setStatus("API 地址必须指向本机 127.0.0.1 或 localhost。", "err");
    return false;
  }
  return true;
}

// 保存设置，避免把采集结果发到奇怪地址。
saveBtn.addEventListener("click", () => {
  const apiUrl = normalizedApi();
  if (!validateApi(apiUrl)) return;
  const autoDetail = autoDetailInput.checked;
  const siteUrl = siteInput.value.trim().replace(/\/+$/, "") || "http://127.0.0.1:8501";
  chrome.storage.local.set({ apiUrl, siteUrl, autoDetail }, () => {
    setStatus(autoDetail ? "设置已保存；浏览 BOSS 页面时会自动补全当前 JD。" : "设置已保存；自动补全未开启。", "ok");
  });
});

// 采集按钮：检查设置 → 让 background 执行当前页提取 → 展示导入结果。
captureBtn.addEventListener("click", () => {
  chrome.storage.local.get(["apiUrl"], (data) => {
    const apiUrl = (data.apiUrl || normalizedApi()).trim().replace(/\/+$/, "");
    if (!apiUrl) {
      setStatus("请先保存 API 地址。", "err");
      return;
    }
    captureBtn.disabled = true;
    setStatus("正在读取当前页……");
    chrome.runtime.sendMessage({ type: "capture", apiUrl }, (response) => {
      captureBtn.disabled = false;
      if (chrome.runtime.lastError) {
        setStatus("扩展内部错误：" + chrome.runtime.lastError.message, "err");
        return;
      }
      if (!response || !response.ok) {
        setStatus((response && response.error) || "采集失败，请查看页面是否已加载完。", "err");
        return;
      }
      const result = response.result || {};
      const inserted = Number(result.inserted || 0);
      const updated = Number(result.updated || 0);
      const skipped = Number(result.skipped || 0);
      const total = inserted + updated;
      const duplicate = total ? Math.round((updated / total) * 100) : 0;
      setStatus(
        "采集完成：" +
          "新增 " + inserted + "｜更新 " + updated + "｜跳过 " + skipped +
          "｜本批重复率 " + duplicate + "%",
        "ok"
      );
    });
  });
});

// 清空本机职位库：只影响当前登录用户的可见性，管理员可恢复。
clearBtn.addEventListener("click", () => {
  chrome.storage.local.get(["apiUrl"], (data) => {
    const apiUrl = (data.apiUrl || normalizedApi()).trim().replace(/\/+$/, "");
    if (!validateApi(apiUrl)) return;
    if (!confirm("确定从你的职位板清空？没有自己副本的人也会看不到这批，自己采过的保留；管理员可以恢复。")) {
      return;
    }
    clearBtn.disabled = true;
    setStatus("正在清空……");
    chrome.storage.local.get(["siteUrl"], (siteData) => {
      const siteUrl = (siteData.siteUrl || "http://127.0.0.1:8501").replace(/\/+$/, "");
      chrome.cookies.get({ url: siteUrl, name: "czx_session" }, (cookie) => {
      const headers = {};
      if (cookie && cookie.value) headers.Authorization = "Bearer " + cookie.value;
      fetch(apiUrl + "/api/jobs/clear", { method: "DELETE", headers })
        .then(async (resp) => {
          const body = await resp.json().catch(() => ({}));
          if (!resp.ok) throw new Error(body.detail || ("HTTP " + resp.status));
          setStatus(body.message || "已从你的职位板清空。", "ok");
        })
        .catch((err) => setStatus("清空失败：" + err.message, "err"))
        .finally(() => {
          clearBtn.disabled = false;
        });
      });
    });
  });
});

// 统一更新状态框颜色，避免每个分支手写 class 字符串。
function setStatus(text, kind) {
  statusBox.textContent = text;
  statusBox.className = "status" + (kind ? " " + kind : "");
}
