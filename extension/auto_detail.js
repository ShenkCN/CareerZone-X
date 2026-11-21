// ============================================================
// BOSS 浏览补全：用户开启后，只在“当前可见详情”变化时补采完整 JD。
// 不自动翻页、不自动点击、不滚动页面；用户自己滚动/打开页面。
// ============================================================
(() => {
  const CITIES = ["上海", "北京", "深圳", "广州", "杭州", "苏州", "南京", "成都", "武汉", "西安", "重庆", "天津", "长沙", "郑州", "合肥", "厦门", "青岛", "无锡", "宁波", "东莞", "佛山", "珠海"];
  let enabled = false;
  let lastHash = "";
  let lastSentAt = 0;

  const nodeText = (node) => ((node && (node.innerText || node.textContent)) || "").replace(/\s+/g, " ").trim();

  chrome.storage.local.get(["autoDetail"], (data) => {
    enabled = Boolean(data && data.autoDetail);
  });
  chrome.storage.onChanged.addListener((changes) => {
    if (changes.autoDetail) enabled = Boolean(changes.autoDetail.newValue);
  });

  function visibleDetail() {
    const selectors = [".job-sec-text", ".job-detail-section", ".job-detail-content", ".job-detail", ".detail-content", "[class*=job-detail]"];
    let best = null;
    let bestScore = 0;
    for (const selector of selectors) {
      for (const node of document.querySelectorAll(selector)) {
        const rect = node.getBoundingClientRect();
        if (rect.width < 40 || rect.height < 40) continue;
        const value = nodeText(node);
        if (value.length < 200) continue;
        const keyword = /(职位描述|岗位职责|任职要求|工作职责)/.test(value) ? 4000 : 0;
        const score = value.length + keyword;
        if (score > bestScore) { bestScore = score; best = node; }
      }
    }
    return best;
  }

  function activeJobUrl() {
    if (location.pathname.includes("/job_detail/")) return location.href.split("#")[0];
    const anchor = document.querySelector(".job-card-wrap.active a[href], .job-card-wrapper.active a[href], .job-card.active a[href], li.active a[href]");
    if (anchor) {
      try { return new URL(anchor.getAttribute("href"), location.href).href.split("#")[0]; } catch (e) { return ""; }
    }
    return "";
  }

  function pickText(selectors, root = document) {
    for (const selector of selectors) {
      const node = root.querySelector(selector);
      const value = nodeText(node);
      if (value) return value;
    }
    return "";
  }

  function cityOf(text) {
    return CITIES.find((city) => text.includes(city)) || "";
  }

  function tick() {
    if (!enabled) return;
    const detail = visibleDetail();
    if (!detail) return;
    const description = nodeText(detail);
    if (description.length < 200) return;

    let url = activeJobUrl();
    if (!url) {
      const detailLink = detail.querySelector("a[href*='/job_detail/']");
      if (detailLink) {
        try { url = new URL(detailLink.getAttribute("href"), location.href).href.split("#")[0]; } catch (e) { url = ""; }
      }
    }
    if (!url || !url.includes("job_detail")) return;

    const title = pickText([".job-detail-section h1", ".job-detail h1", ".job-name", "[class*=job-name]", "h1"], detail);
    if (!title) return;

    const hash = url + "|" + description.length + "|" + description.slice(0, 120);
    const now = Date.now();
    if (hash === lastHash || now - lastSentAt < 2500) return;
    lastHash = hash;
    lastSentAt = now;

    const company = pickText([".company-name", "[class*=company-name]", "[class*=company-text]"], detail);
    const salary = pickText([".salary", "[class*=salary]"], detail);
    chrome.runtime.sendMessage({
      type: "auto-detail",
      job: {
        title: title.slice(0, 255),
        company: company.slice(0, 255),
        city: cityOf(description).slice(0, 64),
        salary_text: salary.slice(0, 128),
        url,
        description: description.slice(0, 20000),
        requirements: "",
        source: "boss",
        raw: { capturedBy: "extension", mode: "detail-auto" },
      },
    }, () => void chrome.runtime.lastError);
  }

  // 2.5 秒轮询足够跟上滚动；不监听网络，不触发页面操作。
  setInterval(tick, 2500);
})();
