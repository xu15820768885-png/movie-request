import unittest
from pathlib import Path


class FollowFeatureVisibilityTest(unittest.TestCase):
    def test_native_follow_is_available_without_automatic_transfer(self):
        html = (Path(__file__).parents[1] / "web" / "index.html").read_text(
            encoding="utf-8"
        )

        self.assertIn('id="followTab" class="tab hidden"', html)
        self.assertIn("$('followTab').classList.remove('hidden')", html)
        self.assertIn("我的追更", html)
        self.assertIn("查看自己开启的影巢追更", html)
        self.assertNotIn('data-follow-index="${index}"', html)
        self.assertIn('id="detailFollow"', html)
        self.assertIn("item.series_status==='ongoing'", html)
        self.assertIn("slug:resource.slug", html)
        self.assertIn('class="poster-library-status ${item.in_library?', html)
        self.assertIn("item.in_library?'已入库':'未入库'", html)
        self.assertNotIn("showApp();loadSiteNotice();loadRequests();loadFollows()", html)
        self.assertIn("家人可以直接转存到已选定的 115 文件夹", html)
        self.assertIn("第 1 步：手动选择满意版本并转存", html)
        self.assertIn("下一步：开启追更", html)
        self.assertIn("先转存，再追更", html)
        self.assertIn("data-retry-details", html)
        self.assertIn('data-retry-resource="${provider}"', html)
        self.assertIn("loadDetailResource('hdhive'", html)
        self.assertIn("loadDetailResource('dian'", html)
        self.assertIn("后台独立读取中，不会等待另一个资源源", html)
        self.assertIn("接口速度检测", html)
        self.assertIn("data-test-integration=\"tmdb\"", html)
        self.assertIn("const scope='manual'", html)
        self.assertIn("const actionLabel='转存此资源'", html)
        self.assertIn("function formatResourceSize(value)", html)
        self.assertIn("`${Math.round(gigabytes*10)/10} GB`", html)
        self.assertIn("resource.size_label||formatResourceSize(resource.size_gb)", html)
        self.assertIn('class="resource-tag resource-size" title="文件大小"', html)
        self.assertIn('>文件大小 ${escapeHtml(size)}</span>', html)
        self.assertIn('class="resource-tag resource-subtitle"', html)
        self.assertIn("new AbortController()", html)
        self.assertIn("const searchCache = new Map()", html)
        self.assertIn("Boolean(result.refresh_recommended)", html)
        self.assertIn("&refresh=true", html)
        self.assertIn("const merged=[...fresh,...items]", html)
        self.assertNotIn("function normalizeResource(resource,index,item)", html)
        self.assertIn('id="detailRequest"', html)
        self.assertIn("都不满意，提交求片", html)
        self.assertIn("${requestShortcut}</div>", html)
        self.assertNotIn("安全转存缺失集", html)
        self.assertNotIn("115安全预检", html)
        self.assertNotIn("confirm_whole:", html)
        self.assertNotIn("按实际缺集自动安全补齐", html)


if __name__ == "__main__":
    unittest.main()
