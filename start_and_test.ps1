# 启动服务
Write-Host "正在启动服务..." -ForegroundColor Green
$process = Start-Process -NoNewWindow -PassThru -FilePath "python" -ArgumentList "-m uvicorn main:app --host 127.0.0.1 --port 8091 --reload"
Start-Sleep -Seconds 5

Write-Host "服务已启动，正在测试 API..." -ForegroundColor Green

# 测试 /v1/chat/completions 端点
$url = "http://127.0.0.1:8091/v1/chat/completions"
$headers = @{
    "Content-Type" = "application/json"
    "Authorization" = "Bearer 1"
}
$body = @{
    model = "gemini30pro"
    messages = @(@{role = "user"; content = "Hello"})
    stream = $false
} | ConvertTo-Json

try {
    $response = Invoke-RestMethod -Uri $url -Method Post -Headers $headers -Body $body -ErrorAction Stop
    Write-Host "✅ API 测试成功！响应:" -ForegroundColor Green
    $response | ConvertTo-Json -Depth 10
} catch {
    Write-Host "❌ API 测试失败:" -ForegroundColor Red
    Write-Host "状态码: $($_.Exception.Response.StatusCode.value__)" -ForegroundColor Red
    Write-Host "响应内容: $($_.Exception.Response.Body | Out-String)" -ForegroundColor Red
}

# 停止服务
Write-Host "正在停止服务..." -ForegroundColor Yellow
Stop-Process -Id $process.Id -Force
Write-Host "服务已停止。" -ForegroundColor Green