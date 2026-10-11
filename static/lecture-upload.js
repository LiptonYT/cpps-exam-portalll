(() => {
  const form = document.querySelector('.lecture-create-form');
  if (!form) return;
  const fileInput = form.querySelector('input[name="video_file"]');
  const submitButton = form.querySelector('button[type="submit"]');
  const panel = document.getElementById('lecture-upload-progress');
  const label = document.getElementById('lecture-upload-label');
  const percentLabel = document.getElementById('lecture-upload-percent');
  const track = document.getElementById('lecture-upload-track');
  const bar = document.getElementById('lecture-upload-bar');
  const details = document.getElementById('lecture-upload-details');
  const state = document.getElementById('lecture-upload-state');
  let currentXhr = null;

  const formatBytes = value => {
    if (!Number.isFinite(value) || value < 0) return '—';
    if (value < 1024 * 1024) return Math.max(1, Math.round(value / 1024)) + ' КБ';
    return (value / (1024 * 1024)).toLocaleString('ru-RU', {maximumFractionDigits: 1}) + ' МБ';
  };
  const updateProgress = (percent) => {
    const safePercent = Math.max(0, Math.min(100, Math.round(percent)));
    percentLabel.textContent = safePercent + '%';
    bar.style.width = safePercent + '%';
    track.setAttribute('aria-valuenow', String(safePercent));
  };
  const setFailed = message => {
    label.textContent = 'Загрузка не завершена';
    state.textContent = message;
    state.classList.add('error');
    submitButton.disabled = false;
    submitButton.textContent = 'Добавить видеоурок';
  };

  fileInput.addEventListener('change', () => {
    const file = fileInput.files && fileInput.files[0];
    if (!file) {
      updateProgress(0);
      label.textContent = 'Шкала загрузки видео';
      details.textContent = 'Видео ещё не выбрано';
      state.textContent = 'Выбери видеофайл — после начала загрузки здесь появятся процент и объём.';
      state.classList.remove('error', 'success');
      return;
    }
    panel.hidden = false;
    label.textContent = 'Видео выбрано';
    updateProgress(0);
    details.textContent = file.name + ' · ' + formatBytes(file.size);
    state.textContent = 'Файл готов к загрузке. Нажми «Добавить видеоурок», чтобы начать.';
    state.classList.remove('error', 'success');
  });

  form.addEventListener('submit', event => {
    const file = fileInput.files && fileInput.files[0];
    // Для добавления внешней ссылки оставляем обычную отправку формы.
    if (!file) {
      const urlInput = form.querySelector('input[name="video_url"]');
      if (urlInput && urlInput.value.trim()) {
        label.textContent = 'Добавление видео по ссылке';
        details.textContent = 'Файл с компьютера не загружается, поэтому шкала передачи файла не используется.';
        state.textContent = 'Сохраняем ссылку на видео…';
      }
      return;
    }
    event.preventDefault();
    if (currentXhr) return;

    panel.hidden = false;
    label.textContent = 'Загрузка видео на сервер';
    state.classList.remove('error', 'success');
    state.textContent = 'Не закрывай страницу, пока видео загружается.';
    details.textContent = 'Файл: ' + file.name + ' · Размер: ' + formatBytes(file.size);
    updateProgress(0);
    submitButton.disabled = true;
    submitButton.textContent = 'Загружаем видео…';

    const xhr = new XMLHttpRequest();
    currentXhr = xhr;
    xhr.upload.addEventListener('loadstart', () => {
      label.textContent = 'Загрузка видео на сервер';
      state.textContent = 'Передача началась. Не закрывай страницу.';
    });
    xhr.open('POST', form.action, true);
    xhr.setRequestHeader('X-Requested-With', 'XMLHttpRequest');
    xhr.upload.addEventListener('progress', event => {
      if (!event.lengthComputable || event.total <= 0) {
        details.textContent = 'Передано на сервер: ' + formatBytes(event.loaded);
        return;
      }
      updateProgress(event.loaded / event.total * 100);
      details.textContent = 'Передано на сервер: ' + formatBytes(event.loaded) + ' из ' + formatBytes(event.total);
      if (event.loaded >= event.total) {
        label.textContent = 'Файл передан серверу';
        state.textContent = 'Сервер сохраняет видео и создаёт урок. Ждём подтверждения…';
      }
    });
    xhr.onload = () => {
      currentXhr = null;
      let result = null;
      try { result = JSON.parse(xhr.responseText); } catch (_) {}
      if (xhr.status >= 200 && xhr.status < 300 && result && result.ok) {
        updateProgress(100);
        label.textContent = 'Видео загружено';
        details.textContent = 'Передача файла завершена · ' + formatBytes(file.size);
        state.textContent = '✓ ' + (result.message || 'Видеоурок добавлен') + ' Обновляем список…';
        state.classList.add('success');
        submitButton.textContent = 'Готово';
        window.setTimeout(() => window.location.reload(), 1200);
        return;
      }
      if (xhr.status === 413) {
        setFailed('Файл слишком большой для лимита сервера. По умолчанию установлен лимит запроса 512 МБ, но ограничения хостинга могут быть ниже.');
      } else if (result && result.message) {
        setFailed(result.message);
      } else if (/login/.test(xhr.responseURL || '')) {
        setFailed('Сессия входа истекла. Обнови страницу, войди заново и повтори загрузку.');
      } else {
        setFailed('Ошибка сервера (HTTP ' + xhr.status + '). Проверь лимит загрузки и свободное место на сервере.');
      }
    };
    xhr.onerror = () => {
      currentXhr = null;
      setFailed('Не удалось связаться с сервером. Проверь интернет-соединение и попробуй ещё раз.');
    };
    xhr.onabort = () => {
      currentXhr = null;
      setFailed('Загрузка была прервана. Попробуй ещё раз.');
    };
    xhr.send(new FormData(form));
  });
})();
