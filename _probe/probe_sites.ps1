$ErrorActionPreference = 'Continue'
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

$UA = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36'

$targets = @(
    @{ n = '中国政府网-政策';      u = 'https://www.gov.cn/zhengce/' },
    @{ n = '中国政府网-首页';      u = 'https://www.gov.cn/' },
    @{ n = '民政部';               u = 'https://www.mca.gov.cn/' },
    @{ n = '上海市政府-政策文件';  u = 'https://www.shanghai.gov.cn/nw12344/index.html' },
    @{ n = '北京市政府-政策';      u = 'https://www.beijing.gov.cn/zhengce/' },
    @{ n = '浙江政务服务网';       u = 'https://www.zjzwfw.gov.cn/' },
    @{ n = '广东政务服务网';       u = 'https://www.gdzwfw.gov.cn/' },
    @{ n = '人社部';               u = 'https://www.mohrss.gov.cn/' },
    @{ n = '国家医保局';           u = 'https://www.nhsa.gov.cn/' },
    @{ n = '深圳市政府-政策';      u = 'https://www.sz.gov.cn/zfgb/' }
)

foreach ($t in $targets) {
    $sw = [Diagnostics.Stopwatch]::StartNew()
    try {
        $resp = Invoke-WebRequest -Uri $t.u -UserAgent $UA -TimeoutSec 20 -UseBasicParsing -MaximumRedirection 5
        $sw.Stop()
        $ct = $resp.Headers['Content-Type']
        $len = $resp.RawContentLength
        if (-not $len) { $len = $resp.Content.Length }
        Write-Output ("[OK]   {0,-22} {1}  {2} bytes  {3}ms  ct={4}" -f $t.n, $resp.StatusCode, $len, $sw.ElapsedMilliseconds, $ct)
    }
    catch {
        $sw.Stop()
        $msg = $_.Exception.Message
        if ($msg.Length -gt 90) { $msg = $msg.Substring(0, 90) }
        Write-Output ("[FAIL] {0,-22} {1}ms  {2}" -f $t.n, $sw.ElapsedMilliseconds, $msg)
    }
}
