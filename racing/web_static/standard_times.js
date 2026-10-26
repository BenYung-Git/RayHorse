/** 跑道標準完成時間查表（與 racing/standard_times.py 同步） */
(function (global) {
  const TABLES = {
    ST_TURF: {
      "1000|group": "0.55.95", "1000|1": "0.55.95", "1000|2": "0.56.10", "1000|3": "0.56.45",
      "1000|4": "0.56.85", "1000|5": "0.57.05", "1000|maiden": "0.57.25",
      "1200|group": "1.08.50", "1200|1": "1.08.65", "1200|2": "1.08.95", "1200|3": "1.09.25",
      "1200|4": "1.09.55", "1200|5": "1.09.75", "1200|maiden": "1.10.30",
      "1400|group": "1.21.25", "1400|1": "1.21.40", "1400|2": "1.21.65", "1400|3": "1.21.80",
      "1400|4": "1.22.25", "1400|5": "1.22.45",
      "1600|group": "1.33.95", "1600|1": "1.34.25", "1600|2": "1.34.45", "1600|3": "1.34.65",
      "1600|4": "1.35.30", "1600|5": "1.35.45",
      "1800|group": "1.47.20", "1800|1": "1.47.20", "1800|2": "1.47.40", "1800|3": "1.47.60",
      "1800|4": "1.48.35", "1800|5": "1.48.55",
      "2000|group": "2.01.20", "2000|1": "2.02.10", "2000|2": "2.02.30", "2000|3": "2.02.50",
      "2000|4": "2.03.20", "2000|5": "2.03.45",
      "2200|2": "2.13.50", "2200|3": "2.16.25", "2200|4": "2.16.40", "2200|5": "2.16.95",
      "2400|group": "2.26.90",
    },
    HV_TURF: {
      "1000|2": "0.56.70", "1000|3": "0.57.05", "1000|4": "0.57.25", "1000|5": "0.57.45",
      "1200|1": "1.09.25", "1200|2": "1.09.45", "1200|3": "1.10.00", "1200|4": "1.10.20", "1200|5": "1.10.40",
      "1650|1": "1.39.95", "1650|2": "1.40.10", "1650|3": "1.40.30", "1650|4": "1.40.65", "1650|5": "1.40.85",
      "1800|group": "1.49.00", "1800|1": "1.49.00", "1800|2": "1.49.15", "1800|3": "1.49.65",
      "1800|4": "1.50.50", "1800|5": "1.50.80",
      "2200|3": "2.16.85", "2200|4": "2.17.65", "2200|5": "2.18.10",
    },
    ST_AWT: {
      "1200|1": "1.08.30", "1200|2": "1.08.45", "1200|3": "1.08.65", "1200|4": "1.09.05", "1200|5": "1.09.35",
      "1650|2": "1.37.90", "1650|3": "1.38.15", "1650|4": "1.38.75", "1650|5": "1.39.40",
      "1800|3": "1.47.65", "1800|4": "1.48.40", "1800|5": "1.49.30",
    },
  };

  function parseDistanceM(raw) {
    const m = String(raw || "").match(/(\d{3,4})/);
    return m ? Number(m[1]) : null;
  }

  function parseClassKey(raw) {
    const text = String(raw || "").trim();
    if (!text) return null;
    if (/新馬/.test(text)) return "maiden";
    if (/分級|精英|一級|二級|三級|Group|G\s*[123]|國際/i.test(text)) return "group";
    let m = text.match(/第\s*([一二三四五1-5])\s*班/);
    if (!m) m = text.match(/Class\s*([1-5])/i);
    if (!m) return null;
    const cn = { "一": "1", "二": "2", "三": "3", "四": "4", "五": "5" };
    return cn[m[1]] || m[1];
  }

  function trackKey(venue, area) {
    const v = String(venue || "");
    const a = String(area || "");
    const isAwt = /全天候/i.test(a) || /AWT/i.test(a);
    if (/跑馬地|Happy/i.test(v)) return isAwt ? null : "HV_TURF";
    if (/沙田|Sha/i.test(v)) return isAwt ? "ST_AWT" : "ST_TURF";
    const code = v.trim().toUpperCase();
    if (code === "HV" || code === "H") return isAwt ? null : "HV_TURF";
    if (code === "ST" || code === "S") return isAwt ? "ST_AWT" : "ST_TURF";
    return null;
  }

  function lookupStandardFinishTime(info) {
    if (!info) return "";
    const track = trackKey(info.meeting_venue, info.race_course_area);
    const meters = parseDistanceM(info.distance);
    const cls = parseClassKey(info.race_class);
    if (!track || meters == null || !cls) return "";
    const table = TABLES[track];
    if (!table) return "";
    return table[meters + "|" + cls] || "";
  }

  function attachStandardFinishTime(info) {
    if (!info) return info;
    const out = Object.assign({}, info);
    if (!String(out.standard_finish_time || "").trim()) {
      out.standard_finish_time = lookupStandardFinishTime(out);
    }
    return out;
  }

  global.HppvStandardTimes = {
    lookup: lookupStandardFinishTime,
    attach: attachStandardFinishTime,
  };
})(window);
