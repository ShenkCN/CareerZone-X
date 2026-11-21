// ============================================================
// MV3 service worker：
// 1) 收到 popup 的 capture 指令；
// 2) 用 activeTab 权限只在当前标签执行 extractPage；
// 3) 把结果 POST 到本机 FastAPI（本机默认关闭令牌校验）。
// 不申请 <all_urls> 主机权限，也不持久注入 content script。
// ============================================================

// 招聘网站 URL 识别：只用于标记 source，不用于自动导航。
const SITE_HOSTS = {
  "zhipin.com": "boss",
  "liepin.com": "liepin",
  "zhaopin.com": "zhaopin",
  "51job.com": "51job",
  "yupao.com": "yupao",
};

chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
  if (!message) return false;
  if (message.type === "auto-detail") {
    handleAutoDetail(message.job)
      .then((result) => sendResponse({ ok: true, result }))
      .catch((error) => sendResponse({ ok: false, error: error.message || String(error) }));
    return true;
  }
  if (message.type !== "capture") return false;
  handleCapture(message.apiUrl)
    .then((result) => sendResponse({ ok: true, result }))
    .catch((error) => sendResponse({ ok: false, error: error.message || String(error) }));
  // 返回 true 保持异步 sendResponse 通道打开。
  return true;
});

async function authHeaders() {
  const stored = await chrome.storage.local.get(["siteUrl"]);
  const siteUrl = (stored.siteUrl || "http://127.0.0.1:8501").replace(/\/+$/, "");
  try {
    const cookie = await chrome.cookies.get({ url: siteUrl, name: "czx_session" });
    if (cookie && cookie.value) return { Authorization: "Bearer " + cookie.value };
  } catch (e) { /* 没有 cookies 权限/未登录时退回令牌或单用户 */ }
  return {};
}

async function handleAutoDetail(job) {
  if (!job || !job.url || !job.description) throw new Error("auto detail payload incomplete");
  const stored = await chrome.storage.local.get(["apiUrl"]);
  const apiUrl = (stored.apiUrl || "http://127.0.0.1:8000").replace(/\/+$/, "");
  const auth = await authHeaders();
  const response = await fetch(apiUrl + "/api/jobs/import", {
    method: "POST",
    headers: { "Content-Type": "application/json", ...auth },
    body: JSON.stringify({
      source: "boss",
      page_url: job.url,
      mode: "detail",
      jobs: [job],
    }),
  });
  const body = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(body.detail || ("HTTP " + response.status));
  return body;
}


async function handleCapture(apiUrl) {
  const tabs = await chrome.tabs.query({ active: true, currentWindow: true });
  const tab = tabs && tabs[0];
  if (!tab || !tab.id) throw new Error("没有活动标签页");
  // 不用正则判断协议：避免转义层级过多导致语法错误。
  if (!tab.url || (!tab.url.startsWith("http://") && !tab.url.startsWith("https://"))) {
    throw new Error("当前页不是 http/https 页面");
  }

  // func 必须完全自包含：Chrome 会把它序列化后注入页面，不能引用外部变量。
  const injection = await chrome.scripting.executeScript({
    target: { tabId: tab.id },
    func: extractPage,
  });
  const data = injection && injection[0] && injection[0].result;
  if (!data || !data.ok) throw new Error((data && data.error) || "页面提取失败");
  if (!data.jobs || data.jobs.length === 0) {
    throw new Error("没找到职位。请在职位详情页或搜索结果页重试，并确认页面已加载完。");
  }

  const payload = {
    source: data.source,
    page_url: data.pageUrl,
    mode: data.mode,
    jobs: data.jobs.slice(0, 1000),
  };
  const auth = await authHeaders();
  const response = await fetch(apiUrl.replace(/\/+$/, "") + "/api/jobs/import", {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      ...auth,
    },
    body: JSON.stringify(payload),
  });
  const text = await response.text();
  let body = {};
  try { body = JSON.parse(text); } catch (e) { body = { detail: text }; }
  if (!response.ok) {
    throw new Error(body.detail || ("导入失败 HTTP " + response.status));
  }
  return body;
}

// ============================================================
// 页面内执行：只读 DOM，不点击、不滚动、不发请求。
// 优先 JSON-LD（结构化最可靠），其次站点选择器，最后正文兜底。
// ============================================================
function extractPage() {
  const host = location.hostname.toLowerCase();
  const sourceMap = [
    ["zhipin.com", "boss"],
    ["liepin.com", "liepin"],
    ["zhaopin.com", "zhaopin"],
    ["51job.com", "51job"],
    ["yupao.com", "yupao"],
  ];
  let source = "";
  for (const pair of sourceMap) {
    if (host === pair[0] || host.endsWith("." + pair[0])) source = pair[1];
  }
  if (!source) {
    return { ok: false, error: "当前网站不在支持列表：BOSS/猎聘/智联/51JOB/鱼泡" };
  }

  // innerText 不包含 <style>/<script> 的 CSS 文本；textContent 会把样式表一起抓进来。
  const text = (node) => ((node && (node.innerText || node.textContent)) || "").replace(/\s+/g, " ").trim();
  const htmlToText = (value) => {
    const div = document.createElement("div");
    div.innerHTML = value || "";
    return (div.textContent || "").replace(/\s+/g, " ").trim();
  };
  const firstText = (selectors) => {
    for (const selector of selectors) {
      const node = document.querySelector(selector);
      const value = text(node);
      if (value) return value;
    }
    return "";
  };
  // 详情页有多个 class 含 job-detail 的节点；取“最长且像 JD”的那个，
  // 不要拿第一个小节点（曾抓到页面底部“下载App”）。
  const largestJdText = (selectors, minLen = 80) => {
    let best = "";
    let bestScore = 0;
    for (const selector of selectors) {
      for (const node of document.querySelectorAll(selector)) {
        const value = text(node);
        if (value.length < minLen) continue;
        if (/^(下载App|收藏|立即沟通)/.test(value)) continue;
        const keyword = /(职位描述|岗位职责|任职要求|工作职责)/.test(value) ? 4000 : 0;
        const score = value.length + keyword;
        if (score > bestScore) { bestScore = score; best = value; }
      }
    }
    return best;
  };
  const absolute = (href) => {
    try { return new URL(href, location.href).href; } catch (e) { return ""; }
  };
  const cityList = ["北京","上海","广州","深圳","杭州","南京","苏州","无锡","重庆","成都","武汉","西安","长沙","郑州","天津","合肥","厦门","青岛","宁波","东莞","佛山"];
  const findCity = (value) => cityList.find((city) => (value || "").includes(city)) || "";
  const salaryRe = /\d+(?:\.\d+)?\s*[-~—至]\s*\d+(?:\.\d+)?\s*[kK千万]?(?:\s*[·/]\s*(?:月|年|天|时))?/;
  const findSalary = (value) => {
    const match = (value || "").match(salaryRe);
    return match ? match[0] : "";
  };

  // JSON-LD 可能有多层 @graph，递归找 JobPosting。
  const findJobPosting = (node, depth) => {
    if (!node || depth > 6) return null;
    if (Array.isArray(node)) {
      for (const item of node) {
        const found = findJobPosting(item, depth + 1);
        if (found) return found;
      }
      return null;
    }
    if (typeof node !== "object") return null;
    const type = node["@type"];
    if (type === "JobPosting" || (Array.isArray(type) && type.includes("JobPosting"))) return node;
    return findJobPosting(node["@graph"], depth + 1);
  };
  let posting = null;
  const scripts = Array.from(document.querySelectorAll('script[type="application/ld+json"]'));
  for (const script of scripts) {
    try {
      const parsed = JSON.parse(script.textContent || "{}");
      posting = findJobPosting(parsed, 0);
      if (posting) break;
    } catch (e) { /* 单个 JSON-LD 坏了不影响其它 */ }
  }

  const isDetailUrl = /job_detail|\/job\/|jobs\.51job\.com|liepin\.com\/job|yupao\.com\/job/i.test(location.href);
  const pageTitle = document.title || "";
  const bodyText = (document.body && document.body.innerText || "").slice(0, 60000);

  // 详情页：优先 JSON-LD，缺失字段用页面选择器/正文补。
  if (posting || isDetailUrl) {
    const org = posting && posting.hiringOrganization ? posting.hiringOrganization : {};
    const loc = posting && posting.jobLocation ? posting.jobLocation : {};
    const address = loc.address || {};
    const salary = posting && posting.baseSalary ? posting.baseSalary : {};
    const salaryValue = salary.value || {};
    let salaryText = "";
    if (salaryValue.minValue || salaryValue.maxValue) {
      salaryText = [salaryValue.minValue || "", "-", salaryValue.maxValue || "", salary.currency || ""].join("").trim();
      if (salaryValue.unitText) salaryText += " / " + salaryValue.unitText;
    }
    const title = text(document.querySelector("h1")) || (posting && posting.title) || firstText(["[class*=job-title]", "[class*=title]"]) || pageTitle;
    const company = (org && org.name) || firstText(["[class*=company-name]", "[class*=company]", ".company"]) || "";
    const descriptionHtml = (posting && posting.description) || "";
    const description = descriptionHtml ? htmlToText(descriptionHtml) : largestJdText([
      ".job-sec-text", ".job-detail-section", ".job-detail-content", ".job-desc",
      "[class*=job-desc]", "[class*=job-sec]", "[class*=job-detail]", "article", "main"
    ]) || bodyText;
    const requirements = (posting && (posting.qualifications || posting.experienceRequirements)) || "";
    const employment = String((posting && posting.employmentType) || "");
    const jobType = employment.includes("PART") ? "兼职" : (employment.includes("INTERN") ? "实习" : (employment.includes("FULL") ? "全职" : ""));

    return {
      ok: true,
      source,
      mode: "detail",
      pageUrl: location.href,
      jobs: [{
        source_job_id: (posting && posting.identifier && (posting.identifier.value || posting.identifier)) || "",
        url: location.href,
        title: title.slice(0, 255),
        company: String(company).slice(0, 255),
        city: String(address.addressLocality || findCity(bodyText)).slice(0, 64),
        district: String(address.addressRegion || "").slice(0, 64),
        salary_text: String(salaryText || findSalary(bodyText)).slice(0, 128),
        job_type: jobType,
        experience: String((posting && posting.experienceRequirements) || "").slice(0, 32),
        education: String((posting && posting.educationRequirements) || "").slice(0, 32),
        industry: String((org && org.description) || "").slice(0, 128),
        company_size: "",
        address: String([address.streetAddress, address.addressLocality, address.addressRegion].filter(Boolean).join(" ")).slice(0, 512),
        description: description.slice(0, 30000),
        requirements: String(Array.isArray(requirements) ? requirements.join("\n") : requirements).slice(0, 20000),
        tags: [],
        published_at: (posting && posting.datePosted) || null,
        raw: { capturedBy: "extension", pageTitle, jsonLd: Boolean(posting) },
      }],
    };
  }

  // 列表页：只抓当前 DOM 里的职位链接 + 卡片文字；不滚动加载。
  const linkPatterns = [
    /\/job_detail\//i,
    /liepin\.com\/job\//i,
    /jobs\.zhaopin\.com\/.+/i,
    /51job\.com\/.+\/\d+\.html/i,
    /yupao\.com\/job/i,
  ];
  const anchors = Array.from(document.querySelectorAll("a[href]")).filter((a) => {
    const href = absolute(a.getAttribute("href"));
    return href && linkPatterns.some((re) => re.test(href));
  });
  const seen = new Set();
  const jobs = [];
  // 列表页右侧常常已经显示了当前选中职位的完整 JD；顺手一起采，
  // 这样点进详情时至少当前岗位有完整原文，不用再开一次原站。
  const detailSelectors = [".job-detail-section", ".job-detail", ".job-detail-content", ".job-sec-text", ".detail-content", "[class*=job-detail]"];
  let detailNode = null;
  for (const selector of detailSelectors) {
    const node = Array.from(document.querySelectorAll(selector)).find((item) => {
      const rect = item.getBoundingClientRect();
      return rect.width > 40 && rect.height > 40;
    });
    if (node) { detailNode = node; break; }
  }
  const detailText = detailNode ? text(detailNode).slice(0, 20000) : "";
  const detailTitleNode = detailNode ? detailNode.querySelector("h1, .job-name, [class*=job-name], [class*=job-title]") : null;
  const detailTitle = text(detailTitleNode);
  const activeAnchor = document.querySelector(".job-card-wrap.active a[href], .job-card-wrapper.active a[href], .job-card.active a[href], li.active a[href]");
  const activeTitle = text(activeAnchor) || detailTitle;
  const activeUrl = activeAnchor ? absolute(activeAnchor.getAttribute("href")).split("#")[0] : "";

  // 列表页的“查看更多/职位搜索”等按钮不是岗位，跳过，避免污染职位库。
  const junkTitles = new Set(["查看更多信息", "职位搜索", "搜索", "首页", "登录", "注册", "立即沟通", "查看全部", "更多", "下一页", "上一页", "收藏", "投递", "申请职位"]);
  for (const anchor of anchors) {
    const url = absolute(anchor.getAttribute("href")).split("#")[0];
    if (!url || seen.has(url)) continue;
    seen.add(url);
    const card = anchor.closest("li, .job-card, [class*=job-card], [class*=job-item], [class*=card]") || anchor.parentElement;
    const cardText = text(card) || text(anchor);
    const title = text(anchor) || cardText.split(/\s{2,}|\n/)[0] || "";
    if (!title || title.length < 2) continue;
    if (junkTitles.has(title) || title.includes("查看更多") || title.includes("职位搜索")) continue;
    const lines = cardText.split(/\n|\s{2,}/).map((s) => s.trim()).filter(Boolean);
    // BOSS 列表卡片有独立的公司/区域节点；优先取节点，避免把整段卡片文本当公司名。
    const companyNode = card ? card.querySelector("[class*=company-name], .company-name, .company-text, a[href*='/gongsi/']") : null;
    const areaNode = card ? card.querySelector("[class*=job-area], .job-area, [class*=location]") : null;
    const company = text(companyNode) || lines.find((line) => /公司|有限公司|集团|科技|网络/.test(line) && line !== title) || "";
    const address = text(areaNode);
    const isActiveDetail = detailText.length > 200 && (
      (activeUrl && url === activeUrl) ||
      (activeTitle && title === activeTitle)
    );
    jobs.push({
      source_job_id: "",
      url,
      title: title.slice(0, 255),
      company: company.slice(0, 255),
      city: findCity(cardText).slice(0, 64),
      district: "",
      address: address.slice(0, 512),
      salary_text: findSalary(cardText).slice(0, 128),
      job_type: cardText.includes("实习") ? "实习" : (cardText.includes("兼职") ? "兼职" : ""),
      experience: "",
      education: "",
      industry: "",
      company_size: "",
      description: (isActiveDetail ? detailText : cardText).slice(0, 20000),
      requirements: "",
      tags: [],
      published_at: null,
      raw: { capturedBy: "extension", mode: "list", hasDetail: isActiveDetail },
    });
    if (jobs.length >= 1000) break;
  }

  if (!jobs.length) {
    return { ok: false, error: "当前页没有识别到职位链接。请打开具体职位详情页再试。" };
  }
  return { ok: true, source, mode: "list", pageUrl: location.href, jobs };
}
