window.onload = () => {
    const defaultInterval = 10000;
    let failureInterval = defaultInterval;
    const retryButton = document.createElement('button');
    retryButton.type = 'button';
    retryButton.hidden = true;
    retryButton.textContent = '다시 시도';
    document.getElementById('comment').after(retryButton);
    retryButton.addEventListener('click', () => {
        if (retryButton.disabled) return;
        retryButton.hidden = true;
        pollQueueStatus();
    });
    const requestedReturnUrl = new URLSearchParams(location.search).get('returnUrl');
    const safeReturnUrl = requestedReturnUrl && requestedReturnUrl.startsWith('/')
        && !requestedReturnUrl.startsWith('//') && requestedReturnUrl.length <= 2048
        ? requestedReturnUrl : null;

    function showAdmissionRetry() {
        document.getElementById('waitTimer').textContent = '접속을 완료하지 못했습니다.';
        document.getElementById('comment').textContent = '잠시 후 다시 시도해 주세요. 문제가 계속되면 접속 주소와 브라우저의 쿠키 허용 설정을 확인해 주세요.';
        document.getElementById('wrapper').style.display = 'block';
        retryButton.hidden = false;
        retryButton.disabled = true;
        let remaining = 30;
        retryButton.textContent = remaining + '초 후 다시 시도';
        const countdown = setInterval(() => {
            remaining -= 1;
            retryButton.textContent = remaining > 0 ? remaining + '초 후 다시 시도' : '다시 시도';
            if (remaining <= 0) { clearInterval(countdown); retryButton.disabled = false; }
        }, 1000);
    }
    
    async function pollQueueStatus() {
        try {
            const response = await fetch('/_queue/getTicketStatus', {
                method: 'GET',
                credentials: 'same-origin',
                headers: { 'Content-Type': 'application/json' }
            });
            if (!response.ok) {
                const error = new Error(`HTTP error! status: ${response.status}`);
                const body = await response.json().catch(() => ({}));
                error.registrationPaused = response.status === 503
                    && ['QUEUE_REGISTRATION_PAUSED', 'QUEUE_FULL'].includes(body.error);
                error.retryAfterMs = Math.min(60000, Math.max(0, Number(response.headers.get('Retry-After')) || 0) * 1000);
                throw error;
            }
            const data = await response.json();
            failureInterval = defaultInterval;
            if (data.canAccess) {
                // HttpOnly cookies cannot be read by JavaScript. Check that the browser
                // sends admission back before navigating to Web and risking a redirect loop.
                try {
                    const check = await fetch('/_queue/checkAdmissionCookie', {
                        method: 'GET', credentials: 'same-origin', cache: 'no-store'
                    });
                    if (!check.ok || (await check.json()).ready !== true) {
                        showAdmissionRetry();
                        return;
                    }
                } catch (_) {
                    showAdmissionRetry();
                    return;
                }
                location.href = safeReturnUrl || data.accessUrl;
            } else {
                const baseInterval = Math.max(1000, Number(data.refreshRate || 10) * 1000);
                const nextCheck = Math.round(baseInterval * (0.8 + Math.random() * 0.4));
                
                let waitTimeText = "";
                let comment = "";

                //최근 대기한 대기열의 대기 카운트
                if(data.avgWaitTimeStr){
                    const [hours, minutes, seconds] = data.avgWaitTimeStr.split("|").map(Number);
                    // 대기 시간 텍스트 생성
                    waitTimeText = "예상대기시간";
                    if (hours > 0) {
                        waitTimeText += `<span class="time">${hours}시간 ${minutes}분 ${seconds}초</span>`;
                    } else {
                        waitTimeText += `<span class="time">${minutes}분 ${seconds}초</span>`;
                    }

                    comment = `현재 접속량 증가로 인해,<br> <strong class="cnt"><span>${data.ticketAheadCnt}명</span>이 순차적으로 처리 대기 중</strong>입니다.`;
                } else if(data.ticketAheadCnt == 0){
                    waitTimeText = `곧 페이지로 이동합니다.`;
                    comment = "현재 접속자가 많아 처리가 지연되고 있습니다.<br> 잠시만 기다려 주시면 자동으로 접속됩니다.";
                } else {
                    waitTimeText = `현재 대기인원 <span class="time">${data.ticketAheadCnt} 명</span>`;
                    comment = "현재 접속자가 많아 처리가 지연되고 있습니다.<br> 잠시만 기다려 주시면 자동으로 접속됩니다.";
                }
                document.getElementById("waitTimer").innerHTML = waitTimeText;

                document.getElementById("comment").innerHTML = comment;
                setTimeout(pollQueueStatus, nextCheck);
            }
            document.getElementById("wrapper").style.display='block';
        } catch (error) {
            if (error.registrationPaused) {
                document.getElementById('waitTimer').textContent = '현재 신규 대기 접수가 어렵습니다.';
                document.getElementById('comment').textContent = '접속자가 많아 대기열에 등록되지 않았습니다. 잠시 후 다시 시도해 주세요.';
                document.getElementById('wrapper').style.display = 'block';
                retryButton.hidden = false;
                retryButton.disabled = true;
                let remaining = Math.max(5, Math.ceil((error.retryAfterMs || 30000) / 1000));
                retryButton.textContent = remaining + '초 후 다시 시도';
                const countdown = setInterval(() => {
                    remaining -= 1;
                    retryButton.textContent = remaining > 0 ? remaining + '초 후 다시 시도' : '다시 시도';
                    if (remaining <= 0) { clearInterval(countdown); retryButton.disabled = false; }
                }, 1000);
                return;
            }
            console.error('Error:', error);
            failureInterval = Math.min(30000, Math.round(failureInterval * 1.5));
            const retryWithJitter = Math.max(error.retryAfterMs || 0,
                Math.round(failureInterval * (0.8 + Math.random() * 0.4)));
            document.getElementById('waitTimer').textContent = '접속 요청이 많아 잠시 대기 중입니다.';
            document.getElementById('comment').textContent = '잠시 후 자동으로 다시 확인합니다. 화면을 유지해 주세요.';
            document.getElementById('wrapper').style.display = 'block';
            setTimeout(pollQueueStatus, retryWithJitter);
        }
    }

    pollQueueStatus();
};
