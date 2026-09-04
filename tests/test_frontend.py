import unittest
from pathlib import Path


class FrontendFeatureVisibilityTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.html = (
            Path(__file__).parents[1] / "web" / "index.html"
        ).read_text(encoding="utf-8")

    def test_hdhive_admin_probe_is_visible_during_trial(self):
        html = self.html
        self.assertIn('id="followTab" class="tab hidden"', html)
        self.assertNotIn("$('followTab').classList.remove('hidden')", html)
        self.assertIn('id="followLogTab" class="tab hidden"', html)
        self.assertNotIn("['adminTab','followLogTab'", html)
        self.assertIn('id="activityWashesCard" class="activity-card hidden"', html)
        self.assertIn('id="activityFollowsCard" class="activity-card hidden"', html)
        self.assertIn('id="hdhiveMessageInbox" class="message-inbox panel"', html)
        self.assertIn('class="settings-nav-button" type="button" data-storage-panel="hdhive"', html)
        self.assertIn("api('/api/admin/hdhive/status')", html)
        self.assertIn("['tmdb','hdhive','dian']", html)
        self.assertNotIn("loadDetailResource('hdhive'", html)
        self.assertNotIn('data-resource-provider="hdhive"', html)

    def test_only_dian_resources_are_loaded_and_transferred(self):
        html = self.html
        self.assertIn("loadDetailResource('dian'", html)
        self.assertIn("/api/dian/transfer", html)
        self.assertIn("当前仅保留癫影资源", html)
        self.assertNotIn("loadDetailResource('archive'", html)
        self.assertNotIn('data-resource-provider="archive"', html)
        self.assertNotIn("/api/archive/transfer", html)
        self.assertNotIn("archive_id:resource.archive_id", html)
        self.assertNotIn("官组备份", html)
        self.assertIn("const actionLabel='转存此资源'", html)
        self.assertIn("visible.length-8", html)
        self.assertIn("visible=visible.slice(0,8)", html)
        self.assertIn("resource-expand-tool", html)
        self.assertIn('class="resource-tag resource-size" title="文件大小"', html)
        self.assertIn('class="resource-tag resource-subtitle"', html)

    def test_detail_refresh_and_core_admin_controls_remain(self):
        html = self.html
        self.assertIn("refreshMetadata?'?refresh=true':''", html)
        self.assertIn("正在刷新 TMDB…", html)
        self.assertIn("正在刷新 Emby…", html)
        self.assertIn("embyProgressCache", html)
        self.assertIn("episode-progress/${resolved.tmdb_id}?refresh=true", html)
        self.assertIn('id="detailRequest"', html)
        self.assertIn("提交追更/缺集需求", html)
        self.assertIn("管理员会立即在后台收到，并改用 PT 持续追更", html)
        self.assertIn("管理求片/追更", html)
        self.assertIn('id="activityJobsCard"', html)
        self.assertIn('id="activityFailedCard"', html)
        self.assertIn('id="activityCompletedCard"', html)
        self.assertIn("/api/admin/workflow-jobs/reset-stale", html)
        self.assertIn("/api/admin/workflow-jobs/reset-failed", html)
        self.assertIn('id="pansaveApiId"', html)
        self.assertIn('id="p123DeliveryMode"', html)
        self.assertIn('id="settingsEmbyWebhookEnabled"', html)
        self.assertIn("new AbortController()", html)
        self.assertIn("document.visibilityState==='visible'", html)

    def test_detail_does_not_repeat_p123_robot_delivery_note(self):
        self.assertNotIn("123目标只发送解锁后的链接", self.html)

    def test_admin_can_edit_notice_inline(self):
        self.assertIn('id="editNoticeButton"', self.html)
        self.assertIn('id="noticeForm"', self.html)
        self.assertIn("/api/admin/notice", self.html)
        self.assertIn("if(noticeEditing)return", self.html)


if __name__ == "__main__":
    unittest.main()
