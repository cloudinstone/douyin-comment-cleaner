"""Unit tests for Douyin UI hierarchy parsing and state detection."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from douyin_cleaner.parser import Bounds, analyze_screen  # noqa: E402


SENT_COMMENTS_XML = """<?xml version='1.0' encoding='UTF-8' standalone='yes' ?>
<hierarchy rotation="0">
  <node index="0" text="" resource-id="com.ss.android.ugc.aweme:id/tr1" class="android.widget.FrameLayout" package="com.ss.android.ugc.aweme" clickable="false" long-clickable="false" selected="false" bounds="[0,0][1080,2277]">
    <node index="0" text="" resource-id="com.ss.android.ugc.aweme:id/1rl" class="com.ss.android.ugc.aweme.socialnotice.view.diyview.TagScrollGroup" package="com.ss.android.ugc.aweme" clickable="false" long-clickable="false" selected="false" bounds="[0,318][1080,445]">
      <node index="0" text="" resource-id="" class="com.ss.android.ugc.aweme.socialnotice.view.diyview.NoticeSubTabView" package="com.ss.android.ugc.aweme" clickable="true" long-clickable="false" selected="false" bounds="[42,339][276,413]">
        <node index="0" text="全部" resource-id="com.ss.android.ugc.aweme:id/0qf" class="com.bytedance.dux.text.DuxTextView" package="com.ss.android.ugc.aweme" clickable="false" long-clickable="false" selected="false" bounds="[74,351][244,401]" />
      </node>
      <node index="1" text="" resource-id="" class="com.ss.android.ugc.aweme.socialnotice.view.diyview.NoticeSubTabView" package="com.ss.android.ugc.aweme" clickable="true" long-clickable="false" selected="true" bounds="[297,339][531,413]">
        <node index="0" text="发出的评论" resource-id="com.ss.android.ugc.aweme:id/0qf" class="com.bytedance.dux.text.DuxTextView" package="com.ss.android.ugc.aweme" clickable="false" long-clickable="false" selected="true" bounds="[329,351][499,401]" />
      </node>
    </node>
    <node index="1" text="" resource-id="com.ss.android.ugc.aweme:id/ttv" class="com.ss.android.ugc.aweme.socialnotice.view.diyview.NotificationRecyclerView" package="com.ss.android.ugc.aweme" clickable="false" long-clickable="false" selected="false" bounds="[0,445][1080,2277]">
      <node index="0" text="" resource-id="com.ss.android.ugc.aweme:id/tt1" class="androidx.constraintlayout.widget.ConstraintLayout" package="com.ss.android.ugc.aweme" clickable="true" long-clickable="true" selected="false" bounds="[0,445][1080,692]">
        <node index="0" text="测试用户A" resource-id="com.ss.android.ugc.aweme:id/ttp" class="com.bytedance.ies.dmt.ui.widget.DmtTextView" package="com.ss.android.ugc.aweme" clickable="true" long-clickable="false" selected="false" bounds="[221,477][455,535]" />
        <node index="1" text="评论了: 第一条测试评论" resource-id="com.ss.android.ugc.aweme:id/ts4" class="com.ss.android.ugc.aweme.views.MentionTextView" package="com.ss.android.ugc.aweme" clickable="true" long-clickable="true" selected="false" bounds="[221,546][880,659]" />
      </node>
      <node index="1" text="" resource-id="com.ss.android.ugc.aweme:id/tt1" class="androidx.constraintlayout.widget.ConstraintLayout" package="com.ss.android.ugc.aweme" clickable="true" long-clickable="true" selected="false" bounds="[0,692][1080,939]">
        <node index="0" text="测试用户B" resource-id="com.ss.android.ugc.aweme:id/ttp" class="com.bytedance.ies.dmt.ui.widget.DmtTextView" package="com.ss.android.ugc.aweme" clickable="true" long-clickable="false" selected="false" bounds="[221,724][455,782]" />
        <node index="1" text="删除" resource-id="com.ss.android.ugc.aweme:id/ts4" class="com.ss.android.ugc.aweme.views.MentionTextView" package="com.ss.android.ugc.aweme" clickable="true" long-clickable="true" selected="false" bounds="[221,793][880,906]" />
      </node>
    </node>
  </node>
</hierarchy>
"""


class ParserTests(unittest.TestCase):
    def test_bounds_parsing_and_geometry(self) -> None:
        b = Bounds.from_str("[0,445][1080,692]")
        self.assertIsNotNone(b)
        assert b is not None
        self.assertEqual(b.width, 1080)
        self.assertEqual(b.height, 247)
        self.assertEqual(b.center, (540, 568))
        self.assertIsNone(Bounds.from_str("[100,200][100,300]"))

    def test_sent_comments_list_dedupes_nested_long_clickable(self) -> None:
        analysis = analyze_screen(SENT_COMMENTS_XML, 1080, 2340)
        self.assertEqual(analysis.package, "com.ss.android.ugc.aweme")
        self.assertFalse(analysis.is_lockscreen)
        self.assertTrue(analysis.subtab_sent_selected)
        # Comment text literally saying "删除" inside MentionTextView must not trigger popup
        self.assertIsNone(analysis.delete_button)
        self.assertFalse(analysis.has_popup_menu)

        self.assertEqual(len(analysis.comment_rows), 2)
        self.assertEqual(analysis.comment_rows[0].tap_point, (540, 568))
        self.assertIn("评论了: 第一条测试评论", analysis.comment_rows[0].summary)
        self.assertIn("测试用户B", analysis.comment_rows[1].texts)
        self.assertIn("删除", analysis.comment_rows[1].texts)

    def test_unselected_sent_subtab_detected(self) -> None:
        xml = SENT_COMMENTS_XML.replace('selected="true"', 'selected="false"')
        analysis = analyze_screen(xml, 1080, 2340)
        self.assertFalse(analysis.subtab_sent_selected)

    def test_delete_popup_menu_detected(self) -> None:
        xml = """<?xml version='1.0' encoding='UTF-8' ?>
        <hierarchy rotation="0">
          <node text="" class="android.widget.LinearLayout" package="com.ss.android.ugc.aweme" clickable="false" long-clickable="false" selected="false" bounds="[200,900][880,1200]">
            <node text="" class="android.widget.LinearLayout" package="com.ss.android.ugc.aweme" clickable="true" long-clickable="false" selected="false" bounds="[200,900][880,1040]">
              <node text="复制" class="android.widget.TextView" package="com.ss.android.ugc.aweme" clickable="false" long-clickable="false" selected="false" bounds="[260,940][400,1000]" />
            </node>
            <node text="" class="android.widget.LinearLayout" package="com.ss.android.ugc.aweme" clickable="true" long-clickable="false" selected="false" bounds="[200,1040][880,1180]">
              <node text="删除" class="android.widget.TextView" package="com.ss.android.ugc.aweme" clickable="false" long-clickable="false" selected="false" bounds="[260,1080][400,1140]" />
            </node>
          </node>
        </hierarchy>
        """
        analysis = analyze_screen(xml, 1080, 2340)
        self.assertTrue(analysis.has_popup_menu)
        self.assertIsNotNone(analysis.delete_button)
        assert analysis.delete_button is not None
        self.assertEqual(analysis.delete_button, Bounds(200, 1040, 880, 1180))
        self.assertEqual(analysis.comment_rows, [])

    def test_confirm_dialog_prefers_exact_confirm_delete(self) -> None:
        xml = """<?xml version='1.0' encoding='UTF-8' ?>
        <hierarchy rotation="0">
          <node text="" class="android.widget.FrameLayout" package="com.ss.android.ugc.aweme" clickable="false" long-clickable="false" selected="false" bounds="[100,900][980,1400]">
            <node text="确定要删除这条评论吗？" class="android.widget.TextView" package="com.ss.android.ugc.aweme" clickable="false" long-clickable="false" selected="false" bounds="[160,950][920,1060]" />
            <node text="取消" class="android.widget.Button" package="com.ss.android.ugc.aweme" clickable="true" long-clickable="false" selected="false" bounds="[160,1200][500,1320]" />
            <node text="确认删除" class="android.widget.Button" package="com.ss.android.ugc.aweme" clickable="true" long-clickable="false" selected="false" bounds="[560,1200][920,1320]" />
          </node>
        </hierarchy>
        """
        analysis = analyze_screen(xml, 1080, 2340)
        self.assertEqual(analysis.confirm_button, Bounds(560, 1200, 920, 1320))

    def test_rate_limit_and_end_of_list_detection(self) -> None:
        rate_xml = """<?xml version='1.0' encoding='UTF-8' ?>
        <hierarchy rotation="0">
          <node text="操作太频繁，请稍后再试" class="android.widget.TextView" package="com.ss.android.ugc.aweme" clickable="false" long-clickable="false" selected="false" bounds="[200,1100][880,1180]" />
        </hierarchy>
        """
        rate_analysis = analyze_screen(rate_xml, 1080, 2340)
        self.assertEqual(rate_analysis.rate_limit_or_captcha, "操作太频繁，请稍后再试")

        end_xml = """<?xml version='1.0' encoding='UTF-8' ?>
        <hierarchy rotation="0">
          <node text="暂时没有更多了" class="android.widget.TextView" package="com.ss.android.ugc.aweme" clickable="false" long-clickable="false" selected="false" bounds="[350,2000][730,2060]" />
        </hierarchy>
        """
        end_analysis = analyze_screen(end_xml, 1080, 2340)
        self.assertTrue(end_analysis.reached_end)

    def test_lockscreen_detection(self) -> None:
        lock_xml = """<?xml version='1.0' encoding='UTF-8' ?>
        <hierarchy rotation="0">
          <node text="" resource-id="com.android.systemui:id/device_entry_icon_view" class="android.widget.FrameLayout" package="com.android.systemui" clickable="true" long-clickable="false" selected="false" bounds="[450,1683][630,1863]" />
        </hierarchy>
        """
        analysis = analyze_screen(lock_xml, 1080, 2340)
        self.assertTrue(analysis.is_lockscreen)
        self.assertEqual(analysis.comment_rows, [])

    def test_inner_long_clickable_promotes_to_row_container(self) -> None:
        xml = """<?xml version='1.0' encoding='UTF-8' ?>
        <hierarchy rotation="0">
          <node text="" class="androidx.recyclerview.widget.RecyclerView" package="com.ss.android.ugc.aweme" clickable="false" long-clickable="false" selected="false" bounds="[0,445][1080,2200]">
            <node text="" class="android.view.ViewGroup" package="com.ss.android.ugc.aweme" clickable="true" long-clickable="false" selected="false" bounds="[0,500][1080,740]">
              <node text="测试用户C" class="android.widget.TextView" package="com.ss.android.ugc.aweme" clickable="false" long-clickable="false" selected="false" bounds="[220,520][460,575]" />
              <node text="回复了评论: 你好" class="android.widget.TextView" package="com.ss.android.ugc.aweme" clickable="false" long-clickable="true" selected="false" bounds="[220,590][860,690]" />
            </node>
          </node>
        </hierarchy>
        """
        analysis = analyze_screen(xml, 1080, 2340)
        self.assertEqual(len(analysis.comment_rows), 1)
        self.assertEqual(analysis.comment_rows[0].bounds, Bounds(0, 500, 1080, 740))
        self.assertEqual(analysis.comment_rows[0].tap_point, (540, 620))
        self.assertIn("测试用户C", analysis.comment_rows[0].texts)
        self.assertIn("回复了评论: 你好", analysis.comment_rows[0].texts)


if __name__ == "__main__":
    unittest.main()
