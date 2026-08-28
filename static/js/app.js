(function () {
  const MIN_INCREMENT = 5;

  function pad(n) {
    return String(n).padStart(2, "0");
  }

  function formatRemain(ms) {
    if (ms <= 0) return "انتهى المزاد";
    const s = Math.floor(ms / 1000);
    const d = Math.floor(s / 86400);
    const h = Math.floor((s % 86400) / 3600);
    const m = Math.floor((s % 3600) / 60);
    const sec = s % 60;
    if (d > 0) return d + "ي " + pad(h) + ":" + pad(m) + ":" + pad(sec);
    return pad(h) + ":" + pad(m) + ":" + pad(sec);
  }

  function parseEndMs(isoStr) {
    if (!isoStr) return NaN;
    const raw = String(isoStr);
    const s = /Z$/i.test(raw) || /[+-]\d{2}:?\d{2}$/.test(raw) ? raw : raw + "Z";
    return Date.parse(s);
  }

  function westernDigits(value) {
    return String(value || "").replace(/[٠-٩۰-۹]/g, function (ch) {
      const code = ch.charCodeAt(0);
      if (code >= 1632 && code <= 1641) return String(code - 1632);
      if (code >= 1776 && code <= 1785) return String(code - 1776);
      return ch;
    });
  }

  function tickCountdowns() {
    document.querySelectorAll(".countdown[data-ends]").forEach(function (el) {
      const ends = parseEndMs(el.getAttribute("data-ends"));
      if (!ends) return;
      const remain = ends - Date.now();
      el.textContent = formatRemain(remain);
      if (remain <= 0) el.classList.add("ended");
      else el.classList.remove("ended");
    });
    document.querySelectorAll("[data-status-badge]").forEach(function (el) {
      const ends = parseEndMs(el.getAttribute("data-status-ends"));
      if (!ends) return;
      const ended = ends - Date.now() <= 0;
      el.textContent = ended ? "انتهى المزايد" : "متاح للمزايدة";
      el.classList.toggle("badge-ended", ended);
      el.classList.toggle("badge-active", !ended);
    });
  }

  tickCountdowns();
  setInterval(tickCountdowns, 1000);

  function money(n) {
    return Number(n).toLocaleString("en-US", {
      minimumFractionDigits: 2,
      maximumFractionDigits: 2,
    });
  }

  const PRICE_POLL_MS = 4000;
  const URGENT_POLL_MS = 1200;
  let pricePollTimer = null;
  let pricesRefreshing = false;
  let auctionsEtag = "";
  let pricesPollStopped = false;
  const OUTBID_MSG = "قام شخص آخر بوضع مزايدة أعلى منك";

  function storedBidderPhone() {
    try {
      const data = JSON.parse(localStorage.getItem("alfadhli_bidder") || "null");
      return data && data.phone ? String(data.phone).trim() : "";
    } catch (err) {
      return "";
    }
  }

  function applyLeaderBanner(root, data) {
    if (!root) return;
    const text = root.querySelector("[data-leader-text]");
    const hint = root.querySelector("[data-outbid-hint]");
    const outbid = !!(data && data.is_outbid && !data.is_ended);
    root.classList.toggle("is-outbid", outbid);
    if (outbid) {
      root.setAttribute("tabindex", "0");
      root.setAttribute("role", "button");
      if (text) text.textContent = OUTBID_MSG;
      if (hint) hint.hidden = false;
      return;
    }
    root.removeAttribute("tabindex");
    root.setAttribute("role", "status");
    if (hint) hint.hidden = true;
    if (!text) return;
    if (data && data.is_self_leading) {
      text.textContent = "أنت صاحب أعلى مزايدة حالياً";
    } else if (data && data.leading_bidder_name) {
      text.textContent = "صاحب أعلى مزايدة: " + data.leading_bidder_name;
    } else {
      text.textContent = "لا توجد مزايدات بعد";
    }
  }

  function applyAuctionEnd(id, isoStr) {
    if (!id || !isoStr) return;
    document.querySelectorAll('[data-watch-countdown="' + id + '"]').forEach(function (el) {
      el.setAttribute("data-ends", isoStr);
      el.classList.remove("ended");
    });
    document.querySelectorAll('[data-status-watch="' + id + '"]').forEach(function (el) {
      el.setAttribute("data-status-ends", isoStr);
    });
    tickCountdowns();
  }

  function pricePollDelay() {
    let urgent = false;
    document.querySelectorAll(".countdown[data-ends]").forEach(function (el) {
      const ends = parseEndMs(el.getAttribute("data-ends"));
      if (!ends) return;
      const remain = ends - Date.now();
      if (remain > 0 && remain <= 210000) urgent = true;
    });
    return urgent ? URGENT_POLL_MS : PRICE_POLL_MS;
  }

  function openBidForWatch(id) {
    const btn = document.querySelector('[data-open-bid="' + id + '"]');
    openBidModalFromButton(btn);
  }

  async function refreshPrices() {
    const nodes = document.querySelectorAll("[data-watch-price]");
    if (!nodes.length || pricesRefreshing || document.hidden) return;
    pricesRefreshing = true;
    try {
      const headers = { Accept: "application/json" };
      if (auctionsEtag) headers["If-None-Match"] = auctionsEtag;
      const phone = storedBidderPhone();
      if (phone) headers["X-Bidder-Phone"] = phone;
      const res = await fetch("/api/auctions", { headers: headers });
      if (res.status === 304) return;
      if (!res.ok) return;
      const nextTag = res.headers.get("ETag");
      if (nextTag) auctionsEtag = nextTag;
      const rows = await res.json();
      const map = {};
      rows.forEach(function (r) {
        map[r.id] = r;
      });
      let anyLive = false;
      nodes.forEach(function (el) {
        const id = el.getAttribute("data-watch-price");
        const data = map[id];
        if (!data) return;
        if (!data.is_ended) anyLive = true;
        else {
          const endedAt = parseEndMs(data.auction_ends_at || "");
          if (endedAt && Date.now() - endedAt < 30000) anyLive = true;
        }
        el.textContent = money(data.current_price) + " $";
        const minEl = document.querySelector('[data-min-bid="' + id + '"]');
        if (minEl) minEl.textContent = money(data.min_bid);
        const bidBtn = document.querySelector('[data-open-bid="' + id + '"]');
        if (bidBtn) {
          bidBtn.setAttribute("data-min", data.min_bid);
          if (data.bid_increment != null) bidBtn.setAttribute("data-increment", data.bid_increment);
          if (data.current_price != null) bidBtn.setAttribute("data-current", data.current_price);
          bidBtn.setAttribute("data-opening", data.is_opening ? "1" : "0");
        }
        const input = document.querySelector('input[name="amount"][data-watch="' + id + '"]');
        if (input) {
          input.min = data.min_bid;
          input.setAttribute("data-current", data.current_price);
          if (data.bid_increment != null) {
            input.setAttribute("data-increment", data.bid_increment);
            input.step = data.bid_increment;
          }
          if (!input.dataset.touched) input.value = data.min_bid;
        }
        if (data.is_ended) {
          document.querySelectorAll('[data-live="' + id + '"]').forEach(function (btn) {
            btn.disabled = true;
            btn.textContent = "انتهى المزاد";
          });
        } else {
          document.querySelectorAll('[data-live="' + id + '"]').forEach(function (btn) {
            btn.disabled = false;
            if (btn.getAttribute("data-open-bid")) btn.textContent = "وضع مزايدة";
          });
        }
        const bannerRoot = document.querySelector('[data-leader-banner="' + id + '"]');
        if (bannerRoot) applyLeaderBanner(bannerRoot, data);
        if (data.auction_ends_at) applyAuctionEnd(id, data.auction_ends_at);
      });
      if (!anyLive) {
        pricesPollStopped = true;
        if (pricePollTimer) {
          clearTimeout(pricePollTimer);
          pricePollTimer = null;
        }
      }
    } catch (e) {
      /* يبقى العرض الحالي دون إفساد الواجهة */
    } finally {
      pricesRefreshing = false;
    }
  }

  function startPricePolling() {
    if (pricesPollStopped || pricePollTimer) return;
    if (!document.querySelector("[data-watch-price]")) return;
    function loop() {
      if (pricesPollStopped) return;
      pricePollTimer = setTimeout(function () {
        Promise.resolve(refreshPrices()).finally(function () {
          pricePollTimer = null;
          loop();
        });
      }, pricePollDelay());
    }
    loop();
  }

  refreshPrices();
  startPricePolling();
  document.addEventListener("visibilitychange", function () {
    if (document.hidden) return;
    refreshPrices();
  });
  document.addEventListener("click", function (e) {
    const bidBtn = e.target.closest("[data-open-bid]");
    if (bidBtn) {
      e.preventDefault();
      openBidModalFromButton(bidBtn);
      return;
    }
    const bar = e.target.closest("[data-leader-banner].is-outbid");
    if (!bar) return;
    openBidForWatch(bar.getAttribute("data-leader-banner"));
  });
  document.addEventListener("keydown", function (e) {
    if (e.key !== "Enter" && e.key !== " ") return;
    const bar = e.target.closest("[data-leader-banner].is-outbid");
    if (!bar) return;
    e.preventDefault();
    openBidForWatch(bar.getAttribute("data-leader-banner"));
  });

  const modal = document.getElementById("bid-modal");
  const form = document.getElementById("bid-form");
  const errorBox = document.getElementById("bid-error");
  const emailStatus = document.getElementById("bid-email-status");
  const BIDDER_KEY = "alfadhli_bidder";
  let lockedScrollY = 0;

  function lockPageForModal() {
    lockedScrollY = window.scrollY || window.pageYOffset || 0;
    document.documentElement.classList.add("modal-open");
    document.body.classList.add("modal-open");
  }

  function unlockPageForModal() {
    document.documentElement.classList.remove("modal-open");
    document.body.classList.remove("modal-open");
    window.scrollTo(0, lockedScrollY);
  }

  function openBidModalFromButton(btn) {
    if (!modal || !form || !btn || btn.disabled) return;
    const id = btn.getAttribute("data-open-bid");
    const min = btn.getAttribute("data-min") || MIN_INCREMENT;
    const inc = btn.getAttribute("data-increment") || MIN_INCREMENT;
    const current = btn.getAttribute("data-current") || "0";
    form.action = "/bid/" + id;
    const amount = form.querySelector('input[name="amount"]');
    amount.min = min;
    amount.step = inc;
    amount.value = min;
    amount.setAttribute("data-watch", id);
    amount.setAttribute("data-increment", inc);
    amount.setAttribute("data-current", current);
    amount.dataset.touched = "";
    if (errorBox) errorBox.hidden = true;
    if (emailStatus) {
      emailStatus.hidden = true;
      emailStatus.textContent = "";
      emailStatus.className = "flash";
    }
    applySavedBidder();
    lockPageForModal();
    modal.classList.add("open");
  }

  function readSavedBidder() {
    try {
      const raw = localStorage.getItem(BIDDER_KEY);
      if (!raw) return null;
      const data = JSON.parse(raw);
      if (!data || typeof data !== "object") return null;
      return {
        full_name: String(data.full_name || "").trim(),
        phone: String(data.phone || "").trim(),
        address: String(data.address || "").trim(),
      };
    } catch (err) {
      return null;
    }
  }

  function saveBidderFromForm() {
    if (!form) return;
    const addressEl = form.querySelector('[name="address"]');
    const saved = readSavedBidder() || {};
    const payload = {
      full_name: (form.querySelector('input[name="full_name"]').value || "").trim(),
      phone: (form.querySelector('input[name="phone"]').value || "").trim(),
      address: addressEl ? (addressEl.value || "").trim() : saved.address || "",
    };
    if (!payload.full_name || !payload.phone) return;
    try {
      localStorage.setItem(BIDDER_KEY, JSON.stringify(payload));
    } catch (err) {
      /* المتصفح قد يمنع التخزين في الوضع الخاص */
    }
  }

  function applySavedBidder() {
    if (!form) return false;
    const saved = readSavedBidder();
    if (!saved || !saved.full_name) return false;
    const name = form.querySelector('input[name="full_name"]');
    const phone = form.querySelector('input[name="phone"]');
    const address = form.querySelector('[name="address"]');
    if (name) name.value = saved.full_name;
    if (phone) phone.value = saved.phone;
    if (address) address.value = saved.address;
    return true;
  }

  function incrementLabel(n) {
    const num = Number(n);
    if (!isNaN(num) && Math.abs(num - Math.round(num)) < 1e-9) return String(Math.round(num));
    return money(n);
  }

  function isValidRaise(val, current, inc) {
    const raise = Math.round((val - current) * 100) / 100;
    if (raise + 1e-9 < inc) return false;
    const q = raise / inc;
    return Math.abs(q - Math.round(q)) < 1e-6;
  }

  function multiplesMessage(inc) {
    return "يجب أن تكون المزايدة بمضاعفات الـ " + incrementLabel(inc) + "$";
  }

  const ADDRESS_DETAIL_ERROR =
    "يجب عليك كتابة المحافظة واسم المنطقة الخاصة بك بدقة، مثل: بغداد - المنصور.";

  function isDetailedAddress(value) {
    const text = String(value || "").trim();
    if (text.length < 6) return false;
    const parts = text.split(/[\s,،;؛\-–—/\\]+/).filter(Boolean);
    let words = 0;
    parts.forEach(function (part) {
      const letters = part.match(/[A-Za-z\u0600-\u06FF]/g) || [];
      if (letters.length >= 2) words += 1;
    });
    return words >= 2;
  }

  document.addEventListener("keydown", function (e) {
    if (e.key === "Escape" && modal && modal.classList.contains("open")) {
      modal.classList.remove("open");
      unlockPageForModal();
    }
  });

  if (form) {
    const amount = form.querySelector('input[name="amount"]');
    const addressLive = form.querySelector('input[name="address"]');
    if (addressLive) {
      addressLive.addEventListener("input", function () {
        if (!errorBox) return;
        const val = (addressLive.value || "").trim();
        if (!val) {
          errorBox.hidden = true;
          return;
        }
        if (!isDetailedAddress(val)) {
          errorBox.hidden = false;
          errorBox.textContent = ADDRESS_DETAIL_ERROR;
        } else if (errorBox.textContent === ADDRESS_DETAIL_ERROR) {
          errorBox.hidden = true;
        }
      });
    }
    if (amount) {
      amount.addEventListener("input", function () {
        amount.dataset.touched = "1";
        const val = parseFloat(amount.value);
        const min = parseFloat(amount.min);
        const inc = parseFloat(amount.getAttribute("data-increment") || MIN_INCREMENT);
        const current = parseFloat(amount.getAttribute("data-current") || "0");
        if (!errorBox) return;
        if (amount.value === "" || isNaN(val)) {
          errorBox.hidden = true;
          return;
        }
        if (val + 1e-9 < min || !isValidRaise(val, current, inc)) {
          errorBox.hidden = false;
          errorBox.textContent = multiplesMessage(inc);
        } else {
          errorBox.hidden = true;
        }
      });
    }
    form.addEventListener("submit", async function (e) {
      e.preventDefault();
      if (form.dataset.submitting === "1") return;
      const nameInput = form.querySelector('input[name="full_name"]');
      const phoneInput = form.querySelector('input[name="phone"]');
      const nameVal = (nameInput && nameInput.value ? nameInput.value : "").trim();
      if (phoneInput) phoneInput.value = westernDigits(phoneInput.value || "").trim();
      const phoneVal = phoneInput ? (phoneInput.value || "").trim() : "";
      const addressInput = form.querySelector('input[name="address"]');
      const addressVal = addressInput ? (addressInput.value || "").trim() : "";
      if (nameVal.length < 3) {
        if (errorBox) {
          errorBox.hidden = false;
          errorBox.textContent = "يرجى إدخال الاسم الكامل.";
        }
        return;
      }
      if (phoneVal.length < 8) {
        if (errorBox) {
          errorBox.hidden = false;
          errorBox.textContent = "يرجى إدخال رقم هاتف صحيح.";
        }
        return;
      }
      if (!isDetailedAddress(addressVal)) {
        if (errorBox) {
          errorBox.hidden = false;
          errorBox.textContent = ADDRESS_DETAIL_ERROR;
        }
        if (addressInput) addressInput.focus();
        return;
      }
      const min = parseFloat(amount.min);
      const val = parseFloat(amount.value);
      const inc = parseFloat(amount.getAttribute("data-increment") || MIN_INCREMENT);
      const current = parseFloat(amount.getAttribute("data-current") || "0");
      if (isNaN(val) || val + 1e-9 < min || !isValidRaise(val, current, inc)) {
        if (errorBox) {
          errorBox.hidden = false;
          errorBox.textContent = multiplesMessage(inc);
        }
        return;
      }
      form.dataset.submitting = "1";
      const submitBtn = form.querySelector('button[type="submit"]');
      if (submitBtn) submitBtn.disabled = true;
      try {
        const body = new FormData(form);
        const res = await fetch(form.action, {
          method: "POST",
          body: body,
          headers: { "X-Requested-With": "fetch", Accept: "application/json" },
        });
        const data = await res.json().catch(function () {
          return { ok: false, error: "تعذر إرسال المزايدة. حاول مرة أخرى." };
        });
        if (!data.ok) {
          if (errorBox) {
            errorBox.hidden = false;
            errorBox.textContent = data.error || "لم يتم قبول المزايدة.";
          }
          return;
        }
        const notice = data.email_notice || "تم تسجيل المزايدة.";
        if (emailStatus) {
          emailStatus.hidden = false;
          emailStatus.textContent = notice;
          emailStatus.className = data.email_ok ? "flash success" : "flash error";
        }
        const pageFlash = document.createElement("div");
        pageFlash.className = "flash " + (data.email_ok ? "success" : "error");
        pageFlash.textContent = notice;
        const main = document.querySelector("main");
        if (main && main.parentNode) main.parentNode.insertBefore(pageFlash, main);
        else document.body.insertBefore(pageFlash, document.body.firstChild);
        saveBidderFromForm();
        refreshPrices();
        applyLeaderBanner(document.querySelector("[data-leader-banner]"), data);
        const watchId = (amount.getAttribute("data-watch") || "").trim();
        if (data.auction_ends_at) applyAuctionEnd(watchId, data.auction_ends_at);
      } catch (err) {
        if (errorBox) {
          errorBox.hidden = false;
          errorBox.textContent = "تعذر إرسال المزايدة. حاول مرة أخرى.";
        }
      } finally {
        form.dataset.submitting = "";
        if (submitBtn) submitBtn.disabled = false;
      }
    });
  }

  document.querySelectorAll("[data-close-modal]").forEach(function (el) {
    el.addEventListener("click", function () {
      if (modal) {
        modal.classList.remove("open");
        unlockPageForModal();
      }
    });
  });

  const drawer = document.getElementById("site-drawer");
  const menuBtn = document.querySelector("[data-open-drawer]");

  function setDrawer(open) {
    document.documentElement.classList.toggle("drawer-open", open);
    document.body.classList.toggle("drawer-open", open);
    if (drawer) drawer.setAttribute("aria-hidden", open ? "false" : "true");
    if (menuBtn) menuBtn.setAttribute("aria-expanded", open ? "true" : "false");
  }

  if (menuBtn) {
    menuBtn.addEventListener("click", function (e) {
      e.stopPropagation();
      setDrawer(!document.body.classList.contains("drawer-open"));
    });
  }
  document.querySelectorAll("[data-close-drawer]").forEach(function (el) {
    el.addEventListener("click", function (e) {
      e.stopPropagation();
      setDrawer(false);
    });
  });
  document.addEventListener("keydown", function (e) {
    if (e.key === "Escape" && !document.body.classList.contains("lightbox-open")) {
      setDrawer(false);
    }
  });

  document.querySelectorAll("[data-gallery]").forEach(function (root) {
    let urls = [];
    try {
      urls = JSON.parse(root.getAttribute("data-images") || "[]");
    } catch (err) {
      urls = [];
    }
    if (!urls.length) return;
    const main = root.querySelector("[data-gallery-main]");
    const dotsBox = root.querySelector("[data-gallery-dots]");
    const lightbox = root.querySelector("[data-lightbox]");
    const lightImg = lightbox ? lightbox.querySelector("[data-lightbox-img]") : null;
    let index = 0;
    let lightScale = 1;
    let panX = 0;
    let panY = 0;

    function preload(i) {
      if (!urls[i]) return;
      const img = new Image();
      img.src = urls[i];
    }

    function setIndex(next) {
      if (!urls.length) return;
      index = (next + urls.length) % urls.length;
      if (main) main.src = urls[index];
      if (lightImg && document.body.classList.contains("lightbox-open")) {
        lightImg.src = urls[index];
      }
      if (dotsBox) {
        Array.prototype.forEach.call(dotsBox.children, function (dot, i) {
          dot.classList.toggle("active", i === index);
        });
      }
      preload((index + 1) % urls.length);
      preload((index - 1 + urls.length) % urls.length);
    }

    function applyZoom() {
      if (!lightImg) return;
      lightImg.style.transform = "translate(" + panX + "px," + panY + "px) scale(" + lightScale + ")";
    }

    function resetZoom() {
      lightScale = 1;
      panX = 0;
      panY = 0;
      applyZoom();
    }

    if (dotsBox) {
      urls.forEach(function (_url, i) {
        const dot = document.createElement("button");
        dot.type = "button";
        dot.setAttribute("aria-label", "صورة " + (i + 1));
        if (i === 0) dot.className = "active";
        dot.addEventListener("click", function () {
          setIndex(i);
        });
        dotsBox.appendChild(dot);
      });
    }

    root.querySelectorAll("[data-gallery-prev]").forEach(function (btn) {
      btn.addEventListener("click", function (e) {
        e.stopPropagation();
        setIndex(index - 1);
      });
    });
    root.querySelectorAll("[data-gallery-next]").forEach(function (btn) {
      btn.addEventListener("click", function (e) {
        e.stopPropagation();
        setIndex(index + 1);
      });
    });

    function openLightbox() {
      if (!lightbox || !lightImg) return;
      lightImg.src = urls[index];
      resetZoom();
      lightbox.classList.add("is-open");
      document.body.classList.add("lightbox-open");
    }

    function closeLightbox() {
      if (!lightbox) return;
      lightbox.classList.remove("is-open");
      document.body.classList.remove("lightbox-open");
      resetZoom();
    }

    let gallerySwipe = null;
    let didSwipe = false;
    const stageMain = root.querySelector(".gallery-stage");
    if (stageMain) {
      stageMain.addEventListener(
        "touchstart",
        function (e) {
          if (e.touches.length === 1) gallerySwipe = e.touches[0].clientX;
        },
        { passive: true }
      );
      stageMain.addEventListener("touchend", function (e) {
        if (gallerySwipe == null || !e.changedTouches[0]) return;
        const dx = e.changedTouches[0].clientX - gallerySwipe;
        gallerySwipe = null;
        if (Math.abs(dx) > 50) {
          didSwipe = true;
          if (dx > 50) setIndex(index - 1);
          else setIndex(index + 1);
        }
      });
    }

    const opener = root.querySelector("[data-gallery-open]");
    if (opener && main) {
      opener.addEventListener("click", function () {
        if (didSwipe) {
          didSwipe = false;
          return;
        }
        openLightbox();
      });
    }

    if (lightbox) {
      const closer = lightbox.querySelector("[data-lightbox-close]");
      if (closer) closer.addEventListener("click", closeLightbox);
      lightbox.addEventListener("click", function (e) {
        if (e.target === lightbox) closeLightbox();
      });
      const zoomBtn = lightbox.querySelector("[data-lightbox-zoom]");
      if (zoomBtn) {
        zoomBtn.addEventListener("click", function (e) {
          e.stopPropagation();
          lightScale = lightScale >= 3.2 ? 1 : Math.min(3.5, lightScale + 0.75);
          if (lightScale === 1) {
            panX = 0;
            panY = 0;
          }
          applyZoom();
        });
      }
      lightbox.querySelectorAll("[data-lightbox-prev]").forEach(function (btn) {
        btn.addEventListener("click", function (e) {
          e.stopPropagation();
          resetZoom();
          setIndex(index - 1);
        });
      });
      lightbox.querySelectorAll("[data-lightbox-next]").forEach(function (btn) {
        btn.addEventListener("click", function (e) {
          e.stopPropagation();
          resetZoom();
          setIndex(index + 1);
        });
      });

      const stage = lightbox.querySelector("[data-lightbox-stage]");
      let pinchStart = 0;
      let pinchScale = 1;
      let lastTouches = null;
      let swipeX = null;

      if (stage) {
        stage.addEventListener(
          "touchstart",
          function (e) {
            if (e.touches.length === 2) {
              const dx = e.touches[0].clientX - e.touches[1].clientX;
              const dy = e.touches[0].clientY - e.touches[1].clientY;
              pinchStart = Math.hypot(dx, dy);
              pinchScale = lightScale;
              swipeX = null;
            } else if (e.touches.length === 1) {
              swipeX = e.touches[0].clientX;
              lastTouches = { x: e.touches[0].clientX, y: e.touches[0].clientY };
            }
          },
          { passive: true }
        );
        stage.addEventListener(
          "touchmove",
          function (e) {
            if (e.touches.length === 2 && pinchStart) {
              const dx = e.touches[0].clientX - e.touches[1].clientX;
              const dy = e.touches[0].clientY - e.touches[1].clientY;
              const dist = Math.hypot(dx, dy);
              lightScale = Math.max(1, Math.min(4, pinchScale * (dist / pinchStart)));
              if (lightScale === 1) {
                panX = 0;
                panY = 0;
              }
              applyZoom();
            } else if (e.touches.length === 1 && lightScale > 1 && lastTouches) {
              panX += e.touches[0].clientX - lastTouches.x;
              panY += e.touches[0].clientY - lastTouches.y;
              lastTouches = { x: e.touches[0].clientX, y: e.touches[0].clientY };
              applyZoom();
            }
          },
          { passive: true }
        );
        stage.addEventListener("touchend", function (e) {
          if (swipeX != null && e.changedTouches[0] && lightScale === 1) {
            const dx = e.changedTouches[0].clientX - swipeX;
            if (dx > 50) setIndex(index - 1);
            else if (dx < -50) setIndex(index + 1);
          }
          swipeX = null;
          pinchStart = 0;
          lastTouches = null;
        });
        stage.addEventListener(
          "wheel",
          function (e) {
            e.preventDefault();
            lightScale = Math.max(1, Math.min(4, lightScale + (e.deltaY < 0 ? 0.2 : -0.2)));
            if (lightScale === 1) {
              panX = 0;
              panY = 0;
            }
            applyZoom();
          },
          { passive: false }
        );
      }
    }

    document.addEventListener("keydown", function (e) {
      if (!document.body.classList.contains("lightbox-open")) return;
      if (e.key === "Escape") closeLightbox();
      if (e.key === "ArrowRight") setIndex(index - 1);
      if (e.key === "ArrowLeft") setIndex(index + 1);
    });

    preload(1);
  });

  document.querySelectorAll("[data-infinite]").forEach(function (grid) {
    const sentinel = grid.nextElementSibling;
    if (!sentinel || !sentinel.hasAttribute("data-infinite-sentinel") || sentinel.hidden) return;
    let offset = Number(grid.getAttribute("data-offset") || 0);
    const type = grid.getAttribute("data-type");
    const page = Number(grid.getAttribute("data-page") || 4);
    let loading = false;
    let done = false;
    const io = new IntersectionObserver(
      function (entries) {
        if (!entries[0] || !entries[0].isIntersecting || loading || done) return;
        loading = true;
        fetch(
          "/api/watches?type=" +
            encodeURIComponent(type) +
            "&offset=" +
            offset +
            "&limit=" +
            page,
          { headers: { Accept: "application/json" } }
        )
          .then(function (res) {
            return res.ok ? res.json() : Promise.reject();
          })
          .then(function (data) {
            if (data.html) grid.insertAdjacentHTML("beforeend", data.html);
            offset = data.next_offset;
            grid.setAttribute("data-offset", String(offset));
            if (data.done) {
              done = true;
              sentinel.hidden = true;
              io.disconnect();
            }
          })
          .catch(function () {})
          .then(function () {
            loading = false;
          });
      },
      { rootMargin: "600px 0px" }
    );
    io.observe(sentinel);
  });

  document.querySelectorAll("[data-image-uploader]").forEach(function (box) {
    const input = box.querySelector("[data-image-picker]");
    const previews = box.querySelector("[data-image-previews]");
    const countEl = box.querySelector("[data-image-count]");
    const pickerBtn = box.querySelector(".image-picker-btn");
    if (!input || !previews) return;
    const max = Number(box.getAttribute("data-max") || 6);
    const chosen = [];
    const urls = [];

    function keptExisting() {
      return previews.querySelectorAll("[data-existing-preview]").length;
    }

    function syncInput() {
      const dt = new DataTransfer();
      chosen.forEach(function (file) {
        dt.items.add(file);
      });
      input.files = dt.files;
    }

    function updateCount() {
      const total = keptExisting() + chosen.length;
      if (countEl) countEl.textContent = total + " / " + max;
      if (pickerBtn) pickerBtn.classList.toggle("disabled", total >= max);
    }

    function addPreview(file, index) {
      const url = URL.createObjectURL(file);
      urls[index] = url;
      const fig = document.createElement("figure");
      fig.className = "image-preview";
      fig.setAttribute("data-new-preview", "1");
      fig.innerHTML =
        '<img alt=""><button type="button" class="image-preview-remove" data-remove-new aria-label="حذف الصورة">×</button>';
      fig.querySelector("img").src = url;
      fig.querySelector("[data-remove-new]").addEventListener("click", function () {
        const i = chosen.indexOf(file);
        if (i >= 0) {
          chosen.splice(i, 1);
          if (urls[i]) URL.revokeObjectURL(urls[i]);
          urls.splice(i, 1);
        }
        fig.remove();
        syncInput();
        updateCount();
      });
      previews.appendChild(fig);
    }

    input.addEventListener("change", function () {
      const picked = Array.from(input.files || []);
      let room = max - keptExisting() - chosen.length;
      let skipped = 0;
      picked.forEach(function (file) {
        const dup = chosen.some(function (f) {
          return f.name === file.name && f.size === file.size && f.lastModified === file.lastModified;
        });
        if (dup) return;
        if (room <= 0) {
          skipped += 1;
          return;
        }
        chosen.push(file);
        addPreview(file, chosen.length - 1);
        room -= 1;
      });
      if (skipped) {
        window.alert("تم بلوغ الحد الأقصى (" + max + " صور). لم تُضف بعض الصور.");
      }
      syncInput();
      updateCount();
    });

    previews.addEventListener("click", function (e) {
      const btn = e.target.closest("[data-remove-existing]");
      if (!btn) return;
      const fig = btn.closest("[data-existing-preview]");
      if (!fig) return;
      const name = fig.getAttribute("data-filename") || "";
      const hidden = document.createElement("input");
      hidden.type = "hidden";
      hidden.name = "remove_existing";
      hidden.value = name;
      box.appendChild(hidden);
      fig.remove();
      updateCount();
    });

    updateCount();
  });
})();
