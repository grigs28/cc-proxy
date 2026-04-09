        var API_BASE = '/api';
        var authToken = sessionStorage.getItem('ccProxyToken') || '';

        // --- 认证 ---

        function login() {
            var password = document.getElementById('password-input').value;
            if (!password) return;
            fetch(API_BASE + '/auth', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ password: password })
            })
            .then(function(r) {
                if (r.ok) return r.json();
                throw new Error('密码错误');
            })
            .then(function(data) {
                authToken = data.token;
                sessionStorage.setItem('ccProxyToken', authToken);
                showApp();
                // 首次启动强制改密码
                if (data.requires_password_change) {
                    setTimeout(function() {
                        showChangePassword();
                        showToast('安全提示：请先修改默认密码', 'warning');
                    }, 500);
                }
            })
            .catch(function(err) {
                showToast(err.message, 'error');
            });
        }

        document.getElementById('password-input').addEventListener('keypress', function(e) {
            if (e.key === 'Enter') login();
        });

        function togglePassword() {
            togglePwdField('password-input');
        }

        function togglePwdField(id) {
            var inp = document.getElementById(id);
            inp.type = inp.type === 'password' ? 'text' : 'password';
        }

        function showApp() {
            document.getElementById('login-overlay').classList.add('hidden');
            document.getElementById('app').style.display = '';
            loadDashboard();
        }

        function logout() {
            sessionStorage.removeItem('ccProxyToken');
            authToken = null;
            document.getElementById('app').style.display = 'none';
            document.getElementById('login-overlay').classList.remove('hidden');
            document.getElementById('password-input').value = '';
            document.getElementById('password-input').focus();
        }

        // 已有 token 时尝试直接显示
        if (authToken) {
            showApp();
        }

        // --- API 请求封装 ---

        function api(url, options) {
            options = options || {};
            var headers = { 'Authorization': 'Bearer ' + authToken };
            if (options.headers) {
                Object.keys(options.headers).forEach(function(k) { headers[k] = options.headers[k]; });
            }
            return fetch(API_BASE + url, { method: options.method || 'GET', headers: headers, body: options.body });
        }

        // --- 工具函数 ---

        function showToast(message, type) {
            type = type || 'success';
            var container = document.getElementById('toast-container');
            var toast = document.createElement('div');
            toast.className = 'toast ' + type;
            toast.textContent = message;
            container.appendChild(toast);
            setTimeout(function() { toast.remove(); }, 3000);
        }

        function formatUptime(seconds) {
            var days = Math.floor(seconds / 86400);
            var hours = Math.floor((seconds % 86400) / 3600);
            var mins = Math.floor((seconds % 3600) / 60);
            var parts = [];
            if (days > 0) parts.push(days + ' 天');
            if (hours > 0) parts.push(hours + ' 小时');
            parts.push(mins + ' 分钟');
            return parts.join(' ');
        }

        function escapeHtml(str) {
            var div = document.createElement('div');
            div.appendChild(document.createTextNode(str));
            return div.innerHTML;
        }

        function escapeAttr(str) {
            return str.replace(/&/g, '&amp;').replace(/'/g, '&#39;').replace(/"/g, '&quot;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
        }

        // --- 标签页切换 ---

        document.querySelectorAll('.nav-tab').forEach(function(tab) {
            tab.addEventListener('click', function() {
                document.querySelectorAll('.nav-tab').forEach(function(t) { t.classList.remove('active'); });
                document.querySelectorAll('.tab-content').forEach(function(c) { c.classList.remove('active'); });
                tab.classList.add('active');
                document.getElementById(tab.dataset.tab + '-tab').classList.add('active');

                if (tab.dataset.tab === 'providers') loadProviders();
                if (tab.dataset.tab === 'models') { loadModels(); }
                if (tab.dataset.tab === 'dashboard') loadDashboard();
            });
        });

        // --- 仪表板 ---

        function loadDashboard() {
            api('/status')
                .then(function(r) { return r.json(); })
                .then(function(data) {
                    document.getElementById('stat-providers').textContent = data.provider_count != null ? data.provider_count : 0;
                    document.getElementById('stat-models').textContent = data.model_count != null ? data.model_count : 0;
                    document.getElementById('stat-port').textContent = data.proxy_port || '-';
                    document.getElementById('info-address').textContent = data.address || '-';
                    document.getElementById('info-proxy-port').textContent = data.proxy_port || '-';
                    document.getElementById('info-admin-port').textContent = data.admin_port || '5566';
                    document.getElementById('info-config-path').textContent = data.config_path || '-';
                    document.getElementById('info-uptime').textContent = data.uptime ? formatUptime(data.uptime) : '-';
                })
                .catch(function(err) { showToast('加载仪表板失败: ' + err.message, 'error'); });
        }

        // --- 提供商管理 ---

        function loadProviders() {
            api('/providers')
                .then(function(r) { return r.json(); })
                .then(function(data) {
                    var tbody = document.getElementById('providers-body');
                    var empty = document.getElementById('providers-empty');
                    tbody.innerHTML = '';

                    var providers = data.providers || [];
                    if (providers.length === 0) {
                        empty.style.display = 'block';
                        document.getElementById('providers-table').style.display = 'none';
                        return;
                    }

                    empty.style.display = 'none';
                    document.getElementById('providers-table').style.display = '';

                    providers.forEach(function(p) {
                        var tr = document.createElement('tr');
                        var safeName = escapeAttr(p.name);
                        var modelCount = (p.models || []).length;
                        var fmts = p.supported_formats || ['openai', 'anthropic'];
                        var fmtBadges = '';
                        if (fmts.includes('openai')) fmtBadges += ' <span class="fmt-badge fmt-openai">O</span>';
                        if (fmts.includes('anthropic')) fmtBadges += ' <span class="fmt-badge fmt-anthropic">A</span>';
                        // 显示 URL：优先用 base_url_openai，其次 base_url_anthropic，回退到 base_url（兼容旧数据）
                        var displayUrl = p.base_url_openai || p.base_url_anthropic || p.base_url || '';
                        var urlTitle = p.base_url_openai && p.base_url_anthropic ? 'OpenAI: ' + p.base_url_openai + '\nAnthropic: ' + p.base_url_anthropic : displayUrl;
                        tr.innerHTML =
                            '<td><strong>' + escapeHtml(p.name) + '</strong>' + fmtBadges + '</td>' +
                            '<td style="max-width:300px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="' + escapeAttr(urlTitle) + '">' + escapeHtml(displayUrl) + '</td>' +
                            '<td class="api-key-cell">' + escapeHtml(p.api_key || '****') + '</td>' +
                            '<td class="timeout-badge">' + p.timeout + 's</td>' +
                            '<td><span class="badge badge-primary">' + modelCount + ' 个</span></td>' +
                            '<td><span class="status-indicator" id="status-' + safeName + '"><span class="status-dot" style="background:var(--text-secondary)"></span> 未知</span></td>' +
                            '<td><div class="actions">' +
                            '<button class="btn btn-secondary btn-sm" onclick="testProvider(\'' + safeName + '\')">测试</button>' +
                            '<button class="btn btn-secondary btn-sm" onclick="editProvider(\'' + safeName + '\')">编辑</button>' +
                            '<button class="btn btn-danger btn-sm" onclick="deleteProvider(\'' + safeName + '\')">删除</button>' +
                            '</div></td>';
                        tbody.appendChild(tr);
                    });
                })
                .catch(function(err) { showToast('加载提供商失败: ' + err.message, 'error'); });
        }

        function updateBaseUrlFields() {
            var enableOpenai = document.getElementById('fmt-openai').checked;
            var enableAnthropic = document.getElementById('fmt-anthropic').checked;
            document.getElementById('provider-url-openai').disabled = !enableOpenai;
            document.getElementById('provider-url-anthropic').disabled = !enableAnthropic;
            // Set required attribute based on enabled state
            document.getElementById('provider-url-openai').required = enableOpenai;
            document.getElementById('provider-url-anthropic').required = enableAnthropic;
        }

        function openProviderModal(provider) {
            var isEdit = !!provider;
            document.getElementById('modal-title').textContent = isEdit ? '编辑提供商' : '添加提供商';
            document.getElementById('provider-editing-name').value = isEdit ? provider.name : '';
            document.getElementById('provider-name').value = isEdit ? provider.name : '';
            // 支持格式
            var fmts = isEdit ? (provider.supported_formats || []) : ['openai', 'anthropic'];
            document.getElementById('fmt-openai').checked = fmts.includes('openai');
            document.getElementById('fmt-anthropic').checked = fmts.includes('anthropic');
            // 更新 base_url 字段可见性
            updateBaseUrlFields();
            // 设置 base_url 值（兼容新旧两种格式）
            if (isEdit) {
                // 优先使用新的分离字段，否则降级到旧的 base_url
                document.getElementById('provider-url-openai').value = provider.base_url_openai || (fmts.includes('openai') && !fmts.includes('anthropic') ? provider.base_url : '') || '';
                document.getElementById('provider-url-anthropic').value = provider.base_url_anthropic || (fmts.includes('anthropic') && !fmts.includes('openai') ? provider.base_url : '') || '';
            } else {
                document.getElementById('provider-url-openai').value = '';
                document.getElementById('provider-url-anthropic').value = '';
            }
            document.getElementById('provider-key').value = isEdit ? provider.api_key : '';
            document.getElementById('provider-timeout').value = isEdit ? provider.timeout : 300;
            document.getElementById('provider-name').disabled = isEdit;
            document.getElementById('provider-modal').classList.add('active');
        }

        function closeProviderModal() {
            document.getElementById('provider-modal').classList.remove('active');
            document.getElementById('provider-form').reset();
            document.getElementById('provider-name').disabled = false;
            document.getElementById('provider-editing-name').value = '';
        }

        function saveProvider() {
            var name = document.getElementById('provider-name').value.trim();
            var base_url_openai = document.getElementById('provider-url-openai').value.trim();
            var base_url_anthropic = document.getElementById('provider-url-anthropic').value.trim();
            var api_key = document.getElementById('provider-key').value.trim();
            var timeout = parseInt(document.getElementById('provider-timeout').value) || 300;
            var editingName = document.getElementById('provider-editing-name').value;

            // 收集 supported_formats
            var fmts = [];
            if (document.getElementById('fmt-openai').checked) fmts.push('openai');
            if (document.getElementById('fmt-anthropic').checked) fmts.push('anthropic');
            if (fmts.length === 0) { showToast('请至少选择一种格式', 'error'); return; }

            // 验证：根据勾选的格式必须有对应的 base_url
            if (fmts.includes('openai') && !base_url_openai) {
                showToast('请填写 OpenAI Base URL', 'error');
                return;
            }
            if (fmts.includes('anthropic') && !base_url_anthropic) {
                showToast('请填写 Anthropic Base URL', 'error');
                return;
            }
            if (!name || !api_key) {
                showToast('请填写所有必填字段', 'error');
                return;
            }

            var isEdit = !!editingName;
            var url = isEdit ? '/providers/' + encodeURIComponent(editingName) : '/providers';
            var method = isEdit ? 'PUT' : 'POST';

            api(url, {
                method: method,
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    name: name,
                    base_url_openai: base_url_openai,
                    base_url_anthropic: base_url_anthropic,
                    api_key: api_key,
                    timeout: timeout,
                    supported_formats: fmts
                })
            })
            .then(function(r) {
                if (r.ok) return r.json();
                return r.json().then(function(e) { throw new Error(e.detail || '操作失败'); });
            })
            .then(function() {
                showToast(isEdit ? '提供商已更新' : '提供商已添加');
                closeProviderModal();
                loadProviders();
            })
            .catch(function(err) { showToast(err.message, 'error'); });
        }

        function testProviderInline() {
            // Provider-level test: no-op now, kept for compatibility
        }

        function detectAuthStyle() {
            var providerName = document.getElementById('modal-add-model-provider').value;
            var modelId = document.getElementById('modal-add-model-id').value.trim();
            if (!providerName || !modelId) {
                showToast('请先选择提供商并输入模型 ID', 'error');
                return;
            }
            var btn = document.getElementById('detect-auth-btn');
            btn.disabled = true;
            btn.textContent = '探测中...';

            api('/providers/detect-auth', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ provider_name: providerName, test_model: modelId })
            })
            .then(function(r) { if (!r.ok) throw new Error('HTTP ' + r.status); return r.json(); })
            .then(function(data) {
                if (data.success && data.best) {
                    document.getElementById('modal-add-model-auth-style').value = data.best;
                    var label = data.best === 'bearer' ? 'Authorization: Bearer' :
                                data.best === 'x-api-key' ? 'x-api-key' : '自动';
                    showToast('探测成功，已选择: ' + label);
                } else {
                    var errors = [];
                    var r = data.results || {};
                    for (var k in r) {
                        if (!r[k].success) errors.push(k + ': HTTP ' + (r[k].status || '') + ' ' + (r[k].error || ''));
                    }
                    showToast('探测失败: ' + (errors.join('; ') || '所有方式均不可用'), 'error');
                }
            })
            .catch(function(err) { showToast('探测失败: ' + err.message, 'error'); })
            .finally(function() {
                btn.disabled = false;
                btn.textContent = '探测';
            });
        }

        function testModelInline() {
            var providerName = document.getElementById('modal-add-model-provider').value;
            var modelId = document.getElementById('modal-add-model-id').value.trim();
            var authStyle = document.getElementById('modal-add-model-auth-style').value;
            if (!providerName || !modelId) {
                showToast('请先选择提供商并输入模型 ID', 'error');
                return;
            }
            var btn = document.getElementById('test-model-btn');
            btn.disabled = true;
            btn.textContent = '测试中...';

            api('/models/test', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ provider_name: providerName, model_id: modelId, auth_style: authStyle })
            })
            .then(function(r) { if (!r.ok) throw new Error('HTTP ' + r.status); return r.json(); })
            .then(function(data) {
                if (data.success) {
                    showToast('测试成功: ' + (data.response || '有响应'));
                } else {
                    showToast('测试失败: HTTP ' + (data.status || '') + ' ' + (data.error || ''), 'error');
                }
            })
            .catch(function(err) { showToast('测试失败: ' + err.message, 'error'); })
            .finally(function() {
                btn.disabled = false;
                btn.textContent = '测试';
            });
        }

        function editProvider(name) {
            api('/providers/' + encodeURIComponent(name))
                .then(function(r) {
                    if (r.ok) return r.json();
                    throw new Error('提供商未找到');
                })
                .then(function(data) { openProviderModal(data); })
                .catch(function(err) { showToast('加载提供商失败: ' + err.message, 'error'); });
        }

        function deleteProvider(name) {
            if (!confirm('确定要删除提供商 "' + name + '" 吗？此操作不可撤销。')) return;
            api('/providers/' + encodeURIComponent(name), { method: 'DELETE' })
                .then(function(r) {
                    if (r.ok) {
                        showToast('提供商 "' + name + '" 已删除');
                        loadProviders();
                    } else {
                        return r.json().then(function(e) { throw new Error(e.detail || '删除失败'); });
                    }
                })
                .catch(function(err) { showToast(err.message, 'error'); });
        }

        function testProvider(name) {
            var statusEl = document.getElementById('status-' + name);
            if (!statusEl) return;
            statusEl.innerHTML = '<span class="status-dot testing"></span> 测试中...';

            api('/providers/' + encodeURIComponent(name) + '/test', { method: 'POST' })
                .then(function(r) {
                    if (!r.ok) throw new Error('HTTP ' + r.status);
                    return r.json();
                })
                .then(function(data) {
                    var results = data.results || {};
                    var openai = results.openai;
                    var anthropic = results.anthropic;

                    var statusHtml = '';
                    if (openai) {
                        var oIcon = openai.success ? '<span class="status-dot online"></span>' : '<span class="status-dot offline"></span>';
                        var oMethod = openai.method ? '<span class="fmt-badge" style="background:#666;color:#fff;padding:1px 4px;border-radius:3px;font-size:10px">' + openai.method + '</span>' : '';
                        var oText = openai.success ? ('O ' + openai.latency + 'ms') : 'O 离线';
                        statusHtml += oIcon + ' ' + oText + ' ' + oMethod;
                    }
                    if (anthropic) {
                        if (statusHtml) statusHtml += ' &nbsp;';
                        var aIcon = anthropic.success ? '<span class="status-dot online"></span>' : '<span class="status-dot offline"></span>';
                        var aMethod = anthropic.method ? '<span class="fmt-badge" style="background:#666;color:#fff;padding:1px 4px;border-radius:3px;font-size:10px">' + anthropic.method + '</span>' : '';
                        var aText = anthropic.success ? ('A ' + anthropic.latency + 'ms') : 'A 离线';
                        statusHtml += aIcon + ' ' + aText + ' ' + aMethod;
                    }
                    if (!statusHtml) {
                        statusHtml = '<span class="status-dot offline"></span> 无URL';
                    }
                    statusEl.innerHTML = statusHtml;

                    // Toast 通知
                    if (data.success) {
                        showToast('"' + name + '" 连接正常');
                    } else {
                        var errors = [];
                        if (openai && !openai.success) errors.push('OpenAI: ' + (openai.error || '失败'));
                        if (anthropic && !anthropic.success) errors.push('Anthropic: ' + (anthropic.error || '失败'));
                        if (errors.length > 0) {
                            showToast('"' + name + '" 部分连接失败:\n' + errors.join('\n'), 'error');
                        }
                    }
                })
                .catch(function(err) {
                    if (statusEl) statusEl.innerHTML = '<span class="status-dot offline"></span> 离线';
                    showToast('"' + name + '" 测试失败: ' + err.message, 'error');
                });
        }

        // --- 模型管理 ---

        function loadModels() {
            Promise.all([api('/models').then(function(r){return r.json()}), api('/providers').then(function(r){return r.json()})])
                .then(function(results) {
                    var data = results[0];
                    var provData = results[1];
                    var providerTypes = {};
                    var providerFmts = {};
                    (provData.providers || []).forEach(function(p) {
                        providerTypes[p.name] = p.type || 'openai';
                        providerFmts[p.name] = p.supported_formats || ['openai', 'anthropic'];
                    });

                    var tbody = document.getElementById('models-body');
                    var empty = document.getElementById('models-empty');
                    tbody.innerHTML = '';

                    var models = data.models || [];
                    if (models.length === 0) {
                        empty.style.display = 'block';
                        document.getElementById('models-table').style.display = 'none';
                        return;
                    }

                    empty.style.display = 'none';
                    document.getElementById('models-table').style.display = '';

                    models.forEach(function(m) {
                        var tr = document.createElement('tr');
                        var pfmts = providerFmts[m.provider_name] || ['openai', 'anthropic'];
                        var fmts = m.supported_formats || pfmts;
                        var safeId = (m.id).replace(/'/g, "\\'");
                        var safeName = (m.provider_name).replace(/'/g, "\\'");

                        // 判断类型：同时支持两种格式=直通，否则=转换
                        var typeLabel, typeColor;
                        if (fmts.length === 2 || (fmts.includes('openai') && fmts.includes('anthropic'))) {
                            typeLabel = '直通';
                            typeColor = 'rgba(34,197,94,0.2);color:#22c55e';
                        } else if (fmts.includes('openai')) {
                            typeLabel = 'OpenAI转换';
                            typeColor = 'rgba(99,102,241,0.2);color:#818cf8';
                        } else if (fmts.includes('anthropic')) {
                            typeLabel = 'Anthropic转换';
                            typeColor = 'rgba(249,115,22,0.2);color:#f97316';
                        } else {
                            typeLabel = '?';
                            typeColor = 'rgba(255,255,255,0.1);color:#888';
                        }

                        tr.innerHTML =
                            '<td><input type="checkbox" class="model-checkbox" data-model-id="' + escapeHtml(m.id) + '" data-provider="' + escapeHtml(m.provider_name) + '"></td>' +
                            '<td><code>' + escapeHtml(m.id) + '</code></td>' +
                            '<td>' + escapeHtml(m.display_name) + '</td>' +
                            '<td><span class="badge badge-primary">' + escapeHtml(m.provider_name) + '</span></td>' +
                            '<td><span class="badge" style="background:' + typeColor + '">' + typeLabel + '</span></td>' +
                            '<td><span class="status-indicator" id="model-status-' + safeId.replace(/[^a-zA-Z0-9]/g, '_') + '"><span class="status-dot" style="background:var(--text-secondary)"></span></span></td>' +
                            '<td><button class="btn btn-secondary btn-sm" onclick="testModel(\'' + safeName + '\',\'' + safeId + '\')">测试</button> <button class="btn btn-secondary btn-sm" onclick="editModel(\'' + safeName + '\',\'' + safeId + '\',\'' + escapeAttr(m.display_name || m.id) + '\',\'' + fmts.join(',') + '\',\'' + (m.auth_style || 'auto') + '\',' + (m.strip_fields ? 'true' : 'false') + ')">编辑</button> <button class="btn btn-danger btn-sm" onclick="deleteModel(\'' + safeName + '\',\'' + safeId + '\')">删除</button></td>';
                        tbody.appendChild(tr);
                    });
                })
                .catch(function(err) { showToast('加载模型失败: ' + err.message, 'error'); });
        }

        function toggleAllModels(el) {
            document.querySelectorAll('.model-checkbox').forEach(function(cb) { cb.checked = el.checked; });
        }

        function testModel(providerName, modelId) {
            var statusId = 'model-status-' + modelId.replace(/[^a-zA-Z0-9]/g, '_');
            var statusEl = document.getElementById(statusId);
            statusEl.innerHTML = '<span class="status-dot testing"></span> 测试中';
            api('/providers/' + encodeURIComponent(providerName) + '/test', { method: 'POST' })
                .then(function(r) { return r.json(); })
                .then(function(data) {
                    if (data.success) {
                        statusEl.innerHTML = '<span class="status-dot online"></span> 在线';
                        showToast(modelId + ' 连接正常 (' + data.latency + 'ms)');
                    } else {
                        statusEl.innerHTML = '<span class="status-dot offline"></span> 离线';
                        showToast(modelId + ' 连接失败: ' + (data.error || ''), 'error');
                    }
                })
                .catch(function(err) {
                    statusEl.innerHTML = '<span class="status-dot offline"></span> 离线';
                    showToast(modelId + ' 测试失败', 'error');
                });
        }

        function testSelectedModels() {
            var checkboxes = document.querySelectorAll('.model-checkbox:checked');
            if (checkboxes.length === 0) { showToast('请先勾选要测试的模型', 'error'); return; }
            checkboxes.forEach(function(cb) {
                testModel(cb.dataset.provider, cb.dataset.modelId);
            });
        }

        function deleteModel(providerName, modelId) {
            if (!confirm('确定删除模型 "' + modelId + '" 吗？')) return;
            api('/providers/' + encodeURIComponent(providerName) + '/models/' + encodeURIComponent(modelId), { method: 'DELETE' })
                .then(function(r) {
                    if (!r.ok) throw new Error('删除失败');
                    showToast('已删除 ' + modelId);
                    loadModels();
                })
                .catch(function(err) { showToast(err.message, 'error'); loadModels(); });
        }

        function deleteSelectedModels() {
            var checkboxes = document.querySelectorAll('.model-checkbox:checked');
            if (checkboxes.length === 0) { showToast('请先勾选要删除的模型', 'error'); return; }
            if (!confirm('确定删除选中的 ' + checkboxes.length + ' 个模型吗？')) return;
            var promises = [];
            checkboxes.forEach(function(cb) {
                promises.push(
                    api('/providers/' + encodeURIComponent(cb.dataset.provider) + '/models/' + encodeURIComponent(cb.dataset.modelId), { method: 'DELETE' })
                        .then(function(r) {
                            if (!r.ok) throw new Error('删除失败');
                        })
                );
            });
            Promise.all(promises)
                .then(function() {
                    showToast('已删除 ' + checkboxes.length + ' 个模型');
                    loadModels();
                })
                .catch(function(err) { showToast(err.message, 'error'); loadModels(); });
        }

        function manageModels(providerName) {
            document.getElementById('models-provider-name').value = providerName;
            document.getElementById('models-modal-title').textContent = '管理模型 - ' + providerName;
            document.getElementById('new-model-id').value = '';
            document.getElementById('new-model-display').value = '';

            api('/providers/' + encodeURIComponent(providerName))
                .then(function(r) {
                    if (r.ok) return r.json();
                    throw new Error('提供商未找到');
                })
                .then(function(data) {
                    var list = document.getElementById('provider-models-list');
                    list.innerHTML = '';
                    var models = data.models || [];
                    if (models.length === 0) {
                        list.innerHTML = '<span style="color:var(--text-secondary)">暂无模型</span>';
                    } else {
                        models.forEach(function(m) {
                            var chip = document.createElement('div');
                            chip.className = 'model-chip';
                            var safeId = escapeAttr(m.id);
                            var safeProvider = escapeAttr(providerName);
                            var fmts = m.supported_formats || ['openai', 'anthropic'];
                            var badgeHtml = '';
                            if (fmts.length === 2) {
                                badgeHtml = '<span class="fmt-badge fmt-both">A+O</span>';
                            } else if (fmts.includes('openai')) {
                                badgeHtml = '<span class="fmt-badge fmt-openai">O</span>';
                            } else if (fmts.includes('anthropic')) {
                                badgeHtml = '<span class="fmt-badge fmt-anthropic">A</span>';
                            }
                            chip.innerHTML = badgeHtml + '<span>' + escapeHtml(m.display_name || m.id) + '</span>' +
                                '<button onclick="removeModel(\'' + safeProvider + '\', \'' + safeId + '\')" title="删除">&times;</button>';
                            list.appendChild(chip);
                        });
                    }
                    document.getElementById('models-modal').classList.add('active');
                })
                .catch(function(err) { showToast('加载模型失败: ' + err.message, 'error'); });
        }

        function closeModelsModal() {
            document.getElementById('models-modal').classList.remove('active');
        }

        function addModel() {
            var providerName = document.getElementById('models-provider-name').value;
            var modelId = document.getElementById('new-model-id').value.trim();
            var displayName = document.getElementById('new-model-display').value.trim() || modelId;

            if (!modelId) {
                showToast('请输入模型 ID', 'error');
                return;
            }

            // 收集 supported_formats
            var fmts = [];
            if (document.getElementById('new-model-openai').checked) fmts.push('openai');
            if (document.getElementById('new-model-anthropic').checked) fmts.push('anthropic');
            if (fmts.length === 0) { showToast('请至少选择一种格式', 'error'); return; }

            api('/providers/' + encodeURIComponent(providerName) + '/models', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ id: modelId, display_name: displayName, supported_formats: fmts })
            })
            .then(function(r) {
                if (r.ok) return r.json();
                return r.json().then(function(e) { throw new Error(e.detail || '添加失败'); });
            })
            .then(function() {
                showToast('模型 "' + modelId + '" 已添加');
                document.getElementById('new-model-id').value = '';
                document.getElementById('new-model-display').value = '';
                manageModels(providerName);
            })
            .catch(function(err) { showToast(err.message, 'error'); });
        }

        function removeModel(providerName, modelId) {
            if (!confirm('确定删除模型 "' + modelId + '" 吗？')) return;
            api('/providers/' + encodeURIComponent(providerName) + '/models/' + encodeURIComponent(modelId), { method: 'DELETE' })
                .then(function(r) {
                    if (r.ok) {
                        showToast('模型 "' + modelId + '" 已删除');
                        manageModels(providerName);
                    } else {
                        return r.json().then(function(e) { throw new Error(e.detail || '删除失败'); });
                    }
                })
                .catch(function(err) { showToast(err.message, 'error'); });
        }

        // --- 添加模型模态框功能 ---

        function openAddModelModal() {
            document.getElementById('add-model-modal').classList.add('active');
            resetAddModelModal();
            loadAddModelProviderOptions();
        }

        function openEditModelModal(providerName, modelId, displayName, fmts, authStyle, stripFields) {
            document.getElementById('add-model-modal').classList.add('active');
            resetAddModelModal();

            // 设置为编辑模式
            document.getElementById('add-model-modal-title').textContent = '编辑模型';
            document.getElementById('add-model-modal-submit-btn').textContent = '保存';
            document.getElementById('modal-add-model-editing-provider').value = providerName;
            document.getElementById('modal-add-model-editing-id').value = modelId;

            // 加载提供商选项并选中（可修改）
            loadAddModelProviderOptions(providerName);
            document.getElementById('modal-add-model-fetch-group').style.display = 'block';

            // 填充模型信息（可修改）
            document.getElementById('modal-add-model-id').value = modelId;
            document.getElementById('modal-add-model-display').value = displayName || modelId;

            // 设置格式
            document.getElementById('modal-add-model-fmt-openai').checked = fmts.includes('openai');
            document.getElementById('modal-add-model-fmt-anthropic').checked = fmts.includes('anthropic');
            // 设置认证方式
            document.getElementById('modal-add-model-auth-style').value = authStyle || 'auto';
            document.getElementById('modal-add-model-strip-fields').checked = !!stripFields;
        }

        function closeAddModelModal() {
            document.getElementById('add-model-modal').classList.remove('active');
            document.getElementById('modal-add-model-id').disabled = false;
        }

        function resetAddModelModal() {
            document.getElementById('add-model-modal-title').textContent = '添加模型';
            document.getElementById('add-model-modal-submit-btn').textContent = '添加';
            document.getElementById('modal-add-model-editing-provider').value = '';
            document.getElementById('modal-add-model-editing-id').value = '';
            document.getElementById('modal-add-model-provider').innerHTML = '<option value="">-- 请选择提供商 --</option>';
            document.getElementById('modal-add-model-provider').disabled = false;
            document.getElementById('modal-add-model-fetch-group').style.display = 'none';
            document.getElementById('modal-add-model-select-group').style.display = 'none';
            document.getElementById('modal-add-model-upstream-select').innerHTML = '<option value="">-- 手动输入 --</option>';
            document.getElementById('modal-add-model-id').value = '';
            document.getElementById('modal-add-model-id').disabled = false;
            document.getElementById('modal-add-model-display').value = '';
            document.getElementById('modal-add-model-fmt-openai').checked = true;
            document.getElementById('modal-add-model-fmt-anthropic').checked = true;
            document.getElementById('modal-add-model-auth-style').value = 'auto';
            document.getElementById('modal-add-model-strip-fields').checked = false;
            document.getElementById('modal-add-model-fetch-status').textContent = '';
        }

        function loadAddModelProviderOptions(selectedProvider) {
            api('/providers')
                .then(function(r) { return r.json(); })
                .then(function(data) {
                    var sel = document.getElementById('modal-add-model-provider');
                    sel.innerHTML = '<option value="">-- 请选择提供商 --</option>';
                    (data.providers || []).forEach(function(p) {
                        var opt = document.createElement('option');
                        opt.value = p.name;
                        opt.textContent = p.name;
                        if (p.name === selectedProvider) {
                            opt.selected = true;
                        }
                        sel.appendChild(opt);
                    });
                })
                .catch(function(err) { console.error('加载提供商失败', err); });
        }

        function editModel(providerName, modelId, displayName, fmtsStr, authStyle, stripFields) {
            var fmts = fmtsStr.split(',');
            openEditModelModal(providerName, modelId, displayName, fmts, authStyle, !!stripFields);
        }

        function onAddModelProviderChange() {
            var providerName = document.getElementById('modal-add-model-provider').value;
            var fetchGroup = document.getElementById('modal-add-model-fetch-group');
            var selectGroup = document.getElementById('modal-add-model-select-group');

            if (providerName) {
                fetchGroup.style.display = 'block';
            } else {
                fetchGroup.style.display = 'none';
                selectGroup.style.display = 'none';
            }
            // 重置下游选择
            selectGroup.style.display = 'none';
            document.getElementById('modal-add-model-upstream-select').innerHTML = '<option value="">-- 手动输入 --</option>';
            document.getElementById('modal-add-model-id').value = '';
            document.getElementById('modal-add-model-display').value = '';
            document.getElementById('modal-add-model-fetch-status').textContent = '';
        }

        function fetchModelsForAdd() {
            var providerName = document.getElementById('modal-add-model-provider').value;
            if (!providerName) { showToast('请先选择提供商', 'error'); return; }

            var statusEl = document.getElementById('modal-add-model-fetch-status');
            statusEl.textContent = '获取中...';

            var sel = document.getElementById('modal-add-model-upstream-select');
            sel.innerHTML = '<option value="">-- 手动输入 --</option>';

            api('/providers/' + encodeURIComponent(providerName) + '/models')
                .then(function(r) {
                    if (!r.ok) throw new Error('获取失败');
                    return r.json();
                })
                .then(function(data) {
                    var models = data.models || [];
                    if (models.length === 0) {
                        statusEl.textContent = '无可用模型';
                        document.getElementById('modal-add-model-select-group').style.display = 'none';
                        return;
                    }
                    models.forEach(function(m) {
                        var opt = document.createElement('option');
                        opt.value = JSON.stringify({ id: m.id, display_name: m.display_name || m.id });
                        opt.textContent = (m.display_name || m.id) + ' (' + m.id + ')';
                        sel.appendChild(opt);
                    });
                    statusEl.textContent = '获取成功 (' + models.length + ' 个)';
                    document.getElementById('modal-add-model-select-group').style.display = 'block';
                })
                .catch(function(err) {
                    statusEl.textContent = '获取失败';
                    showToast('获取模型列表失败: ' + err.message, 'error');
                });
        }

        function onUpstreamModelSelect() {
            var sel = document.getElementById('modal-add-model-upstream-select');
            var selectedOptions = Array.from(sel.selectedOptions).filter(function(opt) { return opt.value; });

            if (selectedOptions.length === 0) {
                document.getElementById('modal-add-model-id').value = '';
                document.getElementById('modal-add-model-display').value = '';
                return;
            }

            if (selectedOptions.length === 1) {
                // 单选：直接填充到输入框
                var m = JSON.parse(selectedOptions[0].value);
                document.getElementById('modal-add-model-id').value = m.id;
                document.getElementById('modal-add-model-display').value = m.display_name || m.id;
            } else {
                // 多选：显示已选数量
                var ids = selectedOptions.map(function(opt) { var m = JSON.parse(opt.value); return m.id; });
                document.getElementById('modal-add-model-id').value = ids.join(', ');
                document.getElementById('modal-add-model-display').value = '';
            }
        }

        function submitAddModel() {
            var providerName = document.getElementById('modal-add-model-provider').value;
            var modelIdInput = document.getElementById('modal-add-model-id').value.trim();
            var displayName = document.getElementById('modal-add-model-display').value.trim();
            var editingProvider = document.getElementById('modal-add-model-editing-provider').value;
            var editingId = document.getElementById('modal-add-model-editing-id').value;
            var isEdit = !!editingId;

            if (!providerName) { showToast('请选择提供商', 'error'); return; }
            if (!modelIdInput) { showToast('请输入模型 ID', 'error'); return; }

            var fmts = [];
            if (document.getElementById('modal-add-model-fmt-openai').checked) fmts.push('openai');
            if (document.getElementById('modal-add-model-fmt-anthropic').checked) fmts.push('anthropic');
            if (fmts.length === 0) { showToast('请至少选择一种格式', 'error'); return; }

            var authStyle = document.getElementById('modal-add-model-auth-style').value;
            var stripFields = document.getElementById('modal-add-model-strip-fields').checked;
            var modelIds = modelIdInput.split(',').map(function(s) { return s.trim(); }).filter(function(s) { return s; });
            if (modelIds.length === 0) { showToast('请输入有效的模型 ID', 'error'); return; }

            // 先获取该提供商已有模型列表，检查重复
            api('/providers/' + encodeURIComponent(providerName))
                .then(function(r) { return r.json(); })
                .then(function(data) {
                    var existingIds = (data.models || []).map(function(m) { return m.id; });
                    // 编辑模式下，排除当前编辑的模型自身
                    var checkIds = existingIds.filter(function(id) { return !isEdit || id !== editingId; });

                    // 找出已存在和新增的
                    var duplicates = modelIds.filter(function(mid) { return checkIds.includes(mid); });
                    var newIds = modelIds.filter(function(mid) { return !checkIds.includes(mid); });

                    if (duplicates.length > 0) {
                        showToast('模型已存在，跳过: ' + duplicates.join(', '), 'warning');
                    }

                    if (newIds.length === 0) {
                        if (isEdit && modelIds.length === 1 && modelIds[0] === editingId) {
                            // 编辑自身但没有改动，不需要操作
                            showToast('未做任何修改', 'warning');
                        }
                        return;
                    }

                    if (isEdit) {
                        // 编辑模式：先删除原始模型，再添加新模型
                        api('/providers/' + encodeURIComponent(editingProvider) + '/models/' + encodeURIComponent(editingId), { method: 'DELETE' })
                            .then(function() {}, function() {})
                            .then(function() {
                                return doAddModels(providerName, newIds, displayName, fmts, authStyle, stripFields, modelIds);
                            });
                    } else {
                        // 添加模式
                        doAddModels(providerName, newIds, displayName, fmts, authStyle, stripFields, modelIds);
                    }
                })
                .catch(function(err) { showToast(err.message, 'error'); });
        }

        function doAddModels(providerName, modelIds, displayName, fmts, authStyle, stripFields, allIds) {
            var promises = modelIds.map(function(mid) {
                var dname = modelIds.length === 1 && displayName ? displayName : mid;
                return api('/providers/' + encodeURIComponent(providerName) + '/models', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ id: mid, display_name: dname, supported_formats: fmts, auth_style: authStyle, strip_fields: stripFields })
                });
            });

            Promise.all(promises)
                .then(function(results) {
                    var successCount = results.filter(function(r) { return r.ok; }).length;
                    if (successCount === modelIds.length) {
                        showToast('已添加 ' + successCount + ' 个模型');
                    } else {
                        showToast('添加完成: ' + successCount + '/' + modelIds.length + ' 成功', 'warning');
                    }
                    closeAddModelModal();
                    loadModels();
                })
                .catch(function(err) { showToast(err.message, 'error'); });
        }

        // --- 配置重载 ---

        function reloadConfig() {
            api('/config/reload', { method: 'POST' })
                .then(function(r) {
                    if (r.ok) return r.json();
                    throw new Error('重载失败');
                })
                .then(function() {
                    showToast('配置已重新加载');
                    loadDashboard();
                })
                .catch(function(err) { showToast(err.message, 'error'); });
        }

        function showChangePassword() {
            document.getElementById('current-password').value = '';
            document.getElementById('new-password').value = '';
            document.getElementById('confirm-password').value = '';
            document.getElementById('password-modal').classList.add('active');
        }

        function closeChangePassword() {
            document.getElementById('password-modal').classList.remove('active');
        }

        function changePassword() {
            var current = document.getElementById('current-password').value;
            var newPw = document.getElementById('new-password').value;
            var confirm = document.getElementById('confirm-password').value;
            if (!current || !newPw) { showToast('请填写所有字段', 'error'); return; }
            if (newPw !== confirm) { showToast('两次输入的新密码不一致', 'error'); return; }
            if (newPw.length < 8) { showToast('新密码至少 8 个字符', 'error'); return; }
            if (!/[a-zA-Z]/.test(newPw) || !/[0-9]/.test(newPw)) { showToast('密码必须同时包含字母和数字', 'error'); return; }
            api('/auth/password', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ current_password: current, new_password: newPw, confirm_password: confirm })
            })
            .then(function(r) {
                if (r.ok) { showToast('密码已修改，请重新登录'); closeChangePassword(); document.getElementById('app').style.display = 'none'; document.getElementById('login-overlay').style.display = 'flex'; sessionStorage.removeItem('authToken'); }
                else { return r.json().then(function(e) { throw new Error(e.detail || '修改失败'); }); }
            })
            .catch(function(err) { showToast(err.message, 'error'); });
        }

        // --- 初始化由认证流程触发 ---
