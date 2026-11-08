/* UI choices extracted without importing or running the application. */
window.CollectionContract = {
  "_COLS": [
    "",
    "作者",
    "标题",
    "发布日期",
    "时长",
    "点赞",
    "收藏",
    "评论",
    "分享",
    "转录",
    "音视频",
    "封面"
  ],
  "_PARTIAL_COLLECTION_SUMMARY": "未登录时匿名读取，已保存浏览器登录时直接复用；仅采最新一页（约 15～20 条）。",
  "_SORT_ATTR": {
    "1": "author_name",
    "3": "create_time",
    "4": "duration_ms",
    "5": "like",
    "6": "collect",
    "7": "comment",
    "8": "share"
  },
  "_SORT_DESC_DEFAULT": {
    "1": false,
    "3": true,
    "4": true,
    "5": true,
    "6": true,
    "7": true,
    "8": true
  },
  "_SORT_FIELDS": [
    [
      "主页顺序",
      0
    ],
    [
      "发布日期",
      3
    ],
    [
      "点赞",
      5
    ],
    [
      "收藏",
      6
    ],
    [
      "评论",
      7
    ],
    [
      "分享",
      8
    ],
    [
      "时长",
      4
    ],
    [
      "作者",
      1
    ]
  ],
  "_FILTER_FIELDS": [
    [
      "不筛选",
      null,
      "none"
    ],
    [
      "点赞",
      "like",
      "numeric"
    ],
    [
      "收藏",
      "collect",
      "numeric"
    ],
    [
      "评论",
      "comment",
      "numeric"
    ],
    [
      "分享",
      "share",
      "numeric"
    ],
    [
      "时长（秒）",
      "duration_sec",
      "numeric"
    ],
    [
      "作品标题",
      "title",
      "text"
    ],
    [
      "作者名称",
      "author_name",
      "text"
    ],
    [
      "作品 ID",
      "work_id",
      "text"
    ],
    [
      "原作品链接",
      "work_url",
      "text"
    ],
    [
      "平台",
      "platform",
      "enum"
    ],
    [
      "内容类型",
      "content_kind",
      "enum"
    ],
    [
      "封面状态",
      "cover_status",
      "enum"
    ]
  ],
  "_FILTER_OPERATORS": {
    "numeric": [
      [
        "大于 >",
        "gt"
      ],
      [
        "大于等于 ≥",
        "ge"
      ],
      [
        "等于 =",
        "eq"
      ],
      [
        "不等于 ≠",
        "ne"
      ],
      [
        "小于 <",
        "lt"
      ],
      [
        "小于等于 ≤",
        "le"
      ]
    ],
    "text": [
      [
        "包含",
        "contains"
      ],
      [
        "不包含",
        "not_contains"
      ],
      [
        "等于",
        "eq"
      ],
      [
        "不等于",
        "ne"
      ]
    ],
    "enum": [
      [
        "等于",
        "eq"
      ],
      [
        "不等于",
        "ne"
      ]
    ]
  },
  "_ENUM_FILTER_VALUES": {
    "platform": [
      [
        "抖音",
        "douyin"
      ],
      [
        "小红书",
        "xiaohongshu"
      ],
      [
        "B站",
        "bilibili"
      ]
    ],
    "content_kind": [
      [
        "视频",
        "video"
      ],
      [
        "图文",
        "image"
      ],
      [
        "混合",
        "mixed"
      ]
    ],
    "cover_status": [
      [
        "未请求",
        "not_requested"
      ],
      [
        "待下载",
        "url_only"
      ],
      [
        "下载中",
        "pending"
      ],
      [
        "已保存",
        "downloaded"
      ],
      [
        "下载失败",
        "failed"
      ],
      [
        "不可用",
        "unavailable"
      ],
      [
        "隐私跳过",
        "skipped_privacy"
      ]
    ]
  },
  "_DATE_FILTERS": [
    [
      "all",
      "日期不限"
    ],
    [
      "7",
      "近 7 天"
    ],
    [
      "30",
      "近 30 天"
    ],
    [
      "90",
      "近 90 天"
    ],
    [
      "custom",
      "自定义日期"
    ]
  ],
  "_DATA_LOCATION_OPTIONS": [
    [
      "collection",
      "采集数据"
    ],
    [
      "covers",
      "封面"
    ],
    [
      "library",
      "文稿"
    ],
    [
      "media",
      "音视频"
    ]
  ],
  "source": "app/src/ui/collection_page.py",
  "source_sha256": "a983fc8e1f5533790a8a598bd6698470c766a640dc0972c908d924f665fea939",
  get today() {return new Intl.DateTimeFormat("sv-SE", {timeZone:"Asia/Shanghai"}).format(new Date());}
};
