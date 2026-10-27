document.addEventListener('DOMContentLoaded', function () {
	function initializePasswordToggles() {
		const togglePasswordButtons = document.querySelectorAll('.toggle-password')
		togglePasswordButtons.forEach(button => {
			button.addEventListener('click', function () {
				const parent =
					this.closest('.form-group') || this.closest('.password-wrapper')
				if (!parent) return

				const passwordInput = parent.querySelector('input')
				if (!passwordInput) return

				if (passwordInput.type === 'password') {
					passwordInput.type = 'text'
					this.textContent = '🙈'
				} else {
					passwordInput.type = 'password'
					this.textContent = '👁️'
				}
			})
		})
	}

	function setupBotControlForms() {
		const controlForms = document.querySelectorAll(
			'form[action*="start-shop-bot"], form[action*="stop-shop-bot"], form[action*="start-support-bot"], form[action*="stop-support-bot"]'
		)

		controlForms.forEach(form => {
			form.addEventListener('submit', function () {
				const button = form.querySelector('button[type="submit"]')
				if (button) {
					button.disabled = true
					if (form.action.includes('start')) {
						button.textContent = 'Запускаем...'
					} else if (form.action.includes('stop')) {
						button.textContent = 'Останавливаем...'
					}
				}
				setTimeout(function () {
					window.location.reload()
				}, 1000) // 1 second
			})
		})
	}

	function setupConfirmationForms() {
		const confirmationForms = document.querySelectorAll('form[data-confirm]')
		confirmationForms.forEach(form => {
			form.addEventListener('submit', function (event) {
				const message = form.getAttribute('data-confirm')
				if (!confirm(message)) {
					event.preventDefault()
				}
			})
		})
	}

	function setupFlashAutoClose() {
		const flashes = document.querySelectorAll('.flash')
		flashes.forEach(flash => {
			setTimeout(() => {
				flash.style.transition = 'opacity 0.4s ease, transform 0.4s ease'
				flash.style.opacity = '0'
				flash.style.transform = 'translateY(-4px)'
				setTimeout(() => {
					flash.remove()
				}, 450)
			}, 5000)
		})
	}

	let dashboardCharts = []
	let dashboardResizeBound = false
	function initializeDashboardCharts() {
		const canvases = [document.getElementById('transactionsChart'), document.getElementById('revenueChart')]
		if (canvases.some(canvas => !canvas) || typeof CHART_DATA === 'undefined') return
		const errorNode = document.getElementById('dashboard-chart-error')
		if (typeof Chart === 'undefined') {
			if (errorNode) errorNode.hidden = false
			return
		}
		try {
			dashboardCharts.forEach(chart => chart.destroy())
			dashboardCharts = []
			const days = []
			const start = new Date(CHART_META.range_start + 'T00:00:00Z')
			const end = new Date(CHART_META.range_end + 'T00:00:00Z')
			for (const cursor = new Date(start); cursor <= end; cursor.setUTCDate(cursor.getUTCDate() + 1)) {
				days.push(cursor.toISOString().slice(0, 10))
			}
			const labels = days.map(day => day.slice(8, 10) + '.' + day.slice(5, 7))
			const types = { all: 'Все', vless: 'Стандарт', super: 'Супер', old: 'Архивные', hysteria: 'Hysteria · старые' }
			for (const [index, metric, title, color] of [[0, 'transactions', 'Оплаты', '#4dabf7'], [1, 'revenue', 'Выручка ₽', '#51cf66']]) {
				const chart = new Chart(canvases[index].getContext('2d'), {
					type: index ? 'bar' : 'line',
					data: {
						labels,
						datasets: [{ label: title + ' · ' + types[CHART_META.tx_type], data: days.map(day => Number(CHART_DATA[metric]?.[day] || 0)), borderColor: color, backgroundColor: color + '55', borderWidth: 2, tension: 0.15 }]
					},
					options: {
						responsive: true, maintainAspectRatio: false, animation: false,
						interaction: { mode: 'index', intersect: false },
						scales: {
							x: { ticks: { display: true, maxTicksLimit: window.innerWidth <= 470 ? 3 : 10, includeBounds: true, maxRotation: 0, font: { size: 10 } } },
							y: { beginAtZero: true, ticks: { display: true, precision: index ? undefined : 0, maxTicksLimit: 5, font: { size: 10 } } }
						},
						plugins: { legend: { display: false }, tooltip: { callbacks: { title: items => days[items[0]?.dataIndex] + ' · МСК' } } }
					}
				})
				dashboardCharts.push(chart)
			}
			if (errorNode) errorNode.hidden = true
			if (!dashboardResizeBound) {
				window.addEventListener('resize', () => dashboardCharts.forEach(chart => {
					chart.options.scales.x.ticks.maxTicksLimit = window.innerWidth <= 470 ? 3 : 10
					chart.update('none')
				}))
				dashboardResizeBound = true
			}
		} catch (error) {
			if (errorNode) errorNode.hidden = false
		}
	}

		function initializeDashboardRangeModal() {
			const modalOverlay = document.getElementById('dashboard-range-modal')
			const openButton = document.getElementById('dashboard-range-open')
			const closeButton = document.getElementById('dashboard-range-close')
			const cancelButton = document.getElementById('dashboard-range-cancel')
			const inputFrom = document.getElementById('dashboard-date-from')
			const inputTo = document.getElementById('dashboard-date-to')
			if (!modalOverlay || !openButton || !inputFrom || !inputTo) {
				return
			}
			const chartMeta = typeof CHART_META !== 'undefined' ? CHART_META : {}

			let lastFocusedElement = null

			function openModal() {
				if (!inputFrom.value && chartMeta?.range_start) {
					inputFrom.value = chartMeta.range_start
				}
				if (!inputTo.value && chartMeta?.range_end) {
					inputTo.value = chartMeta.range_end
				}
				modalOverlay.hidden = false
				document.body.classList.add('modal-open')
			}

			function closeModal() {
				modalOverlay.hidden = true
				document.body.classList.remove('modal-open')
				if (lastFocusedElement) {
					lastFocusedElement.focus()
				}
			}

			openButton.addEventListener('click', () => {
				lastFocusedElement = openButton
				openModal()
			})

			if (closeButton) closeButton.addEventListener('click', closeModal)
			if (cancelButton) cancelButton.addEventListener('click', closeModal)

			modalOverlay.addEventListener('click', event => {
				if (event.target === modalOverlay) {
					closeModal()
				}
			})

			document.addEventListener('keydown', event => {
				if (event.key === 'Escape' && !modalOverlay.hidden) {
					closeModal()
				}
			})
		}

		function initializeDashboardTransactionsPagination() {
			const section = document.getElementById('dashboard-transactions-section')
			const details = document.getElementById('dashboard-transactions-details')
			if (!section || !details) {
				return
			}

			function restoreTransactionsSectionFromHash() {
				if (window.location.hash !== '#dashboard-transactions-section') {
					return
				}

				details.open = true
				requestAnimationFrame(() => {
					section.scrollIntoView({ block: 'start' })
				})
			}

			restoreTransactionsSectionFromHash()
			window.addEventListener('hashchange', restoreTransactionsSectionFromHash)
		}

		function initializeLiveDataSync() {
			const liveRoot = document.querySelector('[data-live-signature-endpoint]')
			if (!liveRoot) {
				return
			}

			const endpoint = String(liveRoot.dataset.liveSignatureEndpoint || '').trim()
			if (!endpoint) {
				return
			}

			const statusNode = liveRoot.querySelector('[data-live-sync-status]')
			const refreshButton = liveRoot.querySelector('[data-live-sync-refresh]')
			const pollMs = Math.max(8000, Number(liveRoot.dataset.livePollMs || 20000))
			let currentSignature = String(liveRoot.dataset.liveSignature || '').trim()
			let inFlight = false
			const isDashboard = Boolean(document.getElementById('dashboard-metrics'))

			function applyDashboardSnapshot(payload) {
				const parsed = new DOMParser().parseFromString(payload.html, 'text/html')
				document.querySelectorAll('[data-dashboard-region]').forEach(region => {
					const replacement = parsed.getElementById(region.id)
					if (!replacement) return
					const openPayments = new Set(Array.from(region.querySelectorAll('details[data-payment-id][open]'), node => node.dataset.paymentId))
					const scrollLeft = region.querySelector('.table-scroll-shell')?.scrollLeft || 0
					region.innerHTML = replacement.innerHTML
					region.querySelectorAll('details[data-payment-id]').forEach(node => { node.open = openPayments.has(node.dataset.paymentId) })
					const shell = region.querySelector('.table-scroll-shell')
					if (shell) shell.scrollLeft = scrollLeft
				})
				CHART_DATA = payload.chart_data
				CHART_META = payload.chart_meta
				initializeDashboardCharts()
				const url = new URL(window.location.href)
				if (url.searchParams.has('page')) {
					url.searchParams.set('page', payload.current_page)
					history.replaceState(null, '', url)
				}
			}

			function setStatus(text, isError = false) {
				if (!statusNode) {
					return
				}
				statusNode.textContent = text
				statusNode.classList.toggle('is-error', isError)
			}

			async function checkSignature() {
				if (inFlight || document.hidden) {
					return
				}
				inFlight = true
				try {
					const query = window.location.search || ''
					const requestUrl = `${endpoint}${query}`
					const response = await fetch(requestUrl, {
						headers: { Accept: 'application/json' },
						cache: 'no-store',
					})
					if (!response.ok) {
						throw new Error(`HTTP ${response.status}`)
					}
					const payload = await response.json()
					const nextSignature = String(payload.signature || '').trim()
					const checkedAt = String(payload.checked_at || '').trim()
					if (nextSignature && currentSignature && nextSignature !== currentSignature) {
						if (isDashboard && payload.html) {
							applyDashboardSnapshot(payload)
						} else {
							window.location.reload()
							return
						}
					}
					if (nextSignature) {
						currentSignature = nextSignature
						liveRoot.dataset.liveSignature = nextSignature
					}
					setStatus(checkedAt ? `Синхронизировано · ${checkedAt}` : 'Синхронизировано', false)
				} catch (error) {
					setStatus('Проверка свежести недоступна', true)
				} finally {
					inFlight = false
				}
			}

			if (refreshButton) {
				refreshButton.addEventListener('click', event => {
					event.preventDefault()
					if (isDashboard) void checkSignature()
					else window.location.reload()
				})
			}

			window.addEventListener('focus', () => {
				void checkSignature()
			})
			document.addEventListener('visibilitychange', () => {
				if (!document.hidden) {
					void checkSignature()
				}
			})

			setStatus('Проверяю свежесть данных…', false)
			void checkSignature()
			setInterval(() => {
				void checkSignature()
			}, pollMs)
		}

		function initializeUserDetailsModal() {
			const modalOverlay = document.getElementById('user-detail-modal')
			if (!modalOverlay) {
				return
			}

		const closeButton = document.getElementById('user-detail-close')
		const stateNode = document.getElementById('user-detail-state')
		const keysBody = document.getElementById('user-detail-keys-body')
		const transactionsBody = document.getElementById('user-detail-transactions-body')
		const detailTelegramId = document.getElementById('detail-telegram-id')
		const detailUsername = document.getElementById('detail-username')
		const detailStatus = document.getElementById('detail-status')
		const detailTotalSpent = document.getElementById('detail-total-spent')
		const detailRegistrationDate = document.getElementById('detail-registration-date')
		const tableScrollSections = Array.from(modalOverlay.querySelectorAll('[data-table-scroll]'))

		let lastFocusedElement = null
		let scrollHintRafId = null

		function openModal() {
			modalOverlay.hidden = false
			document.body.classList.add('modal-open')
		}

		function closeModal() {
			modalOverlay.hidden = true
			document.body.classList.remove('modal-open')
			if (lastFocusedElement) {
				lastFocusedElement.focus()
			}
		}

		function setState(text, isError = false) {
			stateNode.textContent = text
			stateNode.classList.toggle('user-detail-state-error', isError)
		}

		function updateTableScrollHints() {
			tableScrollSections.forEach(section => {
				const wrapper = section.querySelector('[data-scroll-wrapper]')
				const hint = section.querySelector('[data-scroll-hint]')
				if (!wrapper || !hint) {
					return
				}

				const hasOverflow = wrapper.scrollWidth - wrapper.clientWidth > 12
				const isScrolledToEnd =
					wrapper.scrollLeft + wrapper.clientWidth >= wrapper.scrollWidth - 12

				section.classList.toggle('is-overflowing', hasOverflow)
				section.classList.toggle('is-scrolled-end', !hasOverflow || isScrolledToEnd)
				hint.hidden = !hasOverflow
			})
		}

		function scheduleTableScrollHintUpdate() {
			if (scrollHintRafId !== null) {
				cancelAnimationFrame(scrollHintRafId)
			}
			scrollHintRafId = requestAnimationFrame(() => {
				scrollHintRafId = null
				updateTableScrollHints()
			})
		}

		function formatSpent(value) {
			const amount = Number(value || 0)
			if (!Number.isFinite(amount)) {
				return '0 ₽'
			}
			return `${amount.toLocaleString('ru-RU', {
				minimumFractionDigits: 0,
				maximumFractionDigits: 2,
			})} ₽`
		}

		function renderNoKeysRow(message) {
			keysBody.innerHTML = ''
			const row = document.createElement('tr')
			const cell = document.createElement('td')
			cell.colSpan = 7
			cell.textContent = message
			cell.className = 'user-detail-empty-cell'
			row.appendChild(cell)
			keysBody.appendChild(row)
		}

		function renderNoTransactionsRow(message) {
			if (!transactionsBody) {
				return
			}
			transactionsBody.innerHTML = ''
			const row = document.createElement('tr')
			const cell = document.createElement('td')
			cell.colSpan = 7
			cell.textContent = message
			cell.className = 'user-detail-empty-cell'
			row.appendChild(cell)
			transactionsBody.appendChild(row)
		}

		function appendSourceBadge(container, sourceValue) {
			const source = String(sourceValue || 'main').trim().toLowerCase() === 'reserve' ? 'reserve' : 'main'
			const badge = document.createElement('span')
			badge.className = `bot-source-badge bot-source-badge-${source}`
			badge.textContent = source === 'reserve' ? '🟡 Резервный' : '🟢 Основной'
			container.appendChild(badge)
		}

		function renderKeys(keys) {
			keysBody.innerHTML = ''
			if (!Array.isArray(keys) || keys.length === 0) {
				renderNoKeysRow('У пользователя нет выданных ключей')
				return
			}

			keys.forEach(key => {
				const row = document.createElement('tr')

				const keyEmailCell = document.createElement('td')
				keyEmailCell.className = 'actions-cell user-detail-key-action-cell'
				const keyEmailValue = String(key.key_email || '').trim()
				if (keyEmailValue) {
					const copyEmailButton = document.createElement('button')
					copyEmailButton.type = 'button'
					copyEmailButton.className = 'button button-small button-primary user-detail-copy'
					copyEmailButton.textContent = 'Скопировать'
					copyEmailButton.dataset.key = keyEmailValue
					copyEmailButton.title = keyEmailValue
					copyEmailButton.setAttribute('aria-label', `Скопировать KEY_EMAIL: ${keyEmailValue}`)
					keyEmailCell.appendChild(copyEmailButton)
				} else {
					keyEmailCell.textContent = '—'
				}
				row.appendChild(keyEmailCell)

				const hostCell = document.createElement('td')
				hostCell.className = 'table-primary-text'
				hostCell.textContent = key.host_name || '—'
				row.appendChild(hostCell)

				const purchaseCell = document.createElement('td')
				purchaseCell.className = 'user-detail-date-cell table-date table-mono'
				purchaseCell.textContent = key.purchase_date || '—'
				row.appendChild(purchaseCell)

				const expiryCell = document.createElement('td')
				expiryCell.className = 'user-detail-date-cell table-date table-mono'
				expiryCell.textContent = key.expiry_date || '—'
				row.appendChild(expiryCell)

				const sourceCell = document.createElement('td')
				appendSourceBadge(sourceCell, key.bot_source)
				row.appendChild(sourceCell)

				const statusCell = document.createElement('td')
				const statusBadge = document.createElement('span')
				statusBadge.className = `status-badge ${
					key.is_expired ? 'status-vpn-expired' : 'status-vpn-active'
				}`
				statusBadge.textContent = key.is_expired ? 'Просрочен' : 'Активен'
				statusCell.appendChild(statusBadge)
				row.appendChild(statusCell)

				const actionCell = document.createElement('td')
				actionCell.className = 'actions-cell user-detail-key-action-cell'
				if (key.issued_key_value && !key.is_expired) {
					const copyButton = document.createElement('button')
					copyButton.type = 'button'
					copyButton.className = 'button button-small button-primary user-detail-copy'
					copyButton.textContent = 'Скопировать'
					copyButton.dataset.key = key.issued_key_value
					actionCell.appendChild(copyButton)
				} else {
					actionCell.textContent = '—'
				}
				row.appendChild(actionCell)

				keysBody.appendChild(row)
			})
		}

		function renderTransactions(transactions) {
			if (!transactionsBody) {
				return
			}
			transactionsBody.innerHTML = ''
			if (!Array.isArray(transactions) || transactions.length === 0) {
				renderNoTransactionsRow('У пользователя нет paid-транзакций')
				return
			}

			transactions.forEach(transaction => {
				const row = document.createElement('tr')

				const createdCell = document.createElement('td')
				createdCell.className = 'user-detail-date-cell table-date table-mono'
				createdCell.textContent = transaction.created_date || '—'
				row.appendChild(createdCell)

				const methodCell = document.createElement('td')
				methodCell.className = 'table-primary-text'
				methodCell.textContent = transaction.payment_method || '—'
				row.appendChild(methodCell)

				const hostCell = document.createElement('td')
				hostCell.className = 'table-primary-text'
				hostCell.textContent = transaction.host_name || '—'
				row.appendChild(hostCell)

				const planCell = document.createElement('td')
				planCell.className = 'table-primary-text'
				planCell.textContent = transaction.plan_name || '—'
				row.appendChild(planCell)

				const amountCell = document.createElement('td')
				amountCell.className = 'table-amount'
				amountCell.textContent = formatSpent(transaction.amount_rub || 0)
				row.appendChild(amountCell)

				const sourceCell = document.createElement('td')
				appendSourceBadge(sourceCell, transaction.bot_source)
				row.appendChild(sourceCell)

				const paymentCell = document.createElement('td')
				paymentCell.className = 'table-mono table-secondary-text'
				paymentCell.textContent = transaction.payment_id || '—'
				row.appendChild(paymentCell)

				transactionsBody.appendChild(row)
			})
		}

		async function fetchUserDetails(userId) {
			setState('Загрузка...')
			renderNoKeysRow('Загрузка...')
			renderNoTransactionsRow('Загрузка...')
			detailTelegramId.textContent = '—'
			detailUsername.textContent = '—'
			detailStatus.textContent = '—'
			detailStatus.className = ''
			detailTotalSpent.textContent = '—'
			detailRegistrationDate.textContent = '—'
			openModal()
				try {
					const response = await fetch(`/api/users/${encodeURIComponent(userId)}/detail`, {
						headers: {
							Accept: 'application/json',
						},
						cache: 'no-store',
					})
				if (!response.ok) {
					let errorMessage = 'Не удалось загрузить детали пользователя'
					try {
						const errorPayload = await response.json()
						if (errorPayload?.error) {
							errorMessage = errorPayload.error
						}
					} catch (error) {
						// keep default message
					}
					throw new Error(errorMessage)
				}
				const payload = await response.json()
				const user = payload.user || {}
				const keys = payload.keys || []
				const transactions = payload.transactions || []

				detailTelegramId.textContent = user.telegram_id || '—'
				detailUsername.textContent = user.username ? `@${user.username}` : 'N/A'
				detailStatus.textContent = user.is_banned ? 'Заблокирован' : 'Разрешен'
				detailStatus.className = user.is_banned
					? 'status-badge status-banned'
					: 'status-badge status-active'
				detailTotalSpent.textContent = formatSpent(user.total_spent)
				detailRegistrationDate.textContent = user.registration_date || '—'

				setState(`Ключей: ${keys.length} · Оплат: ${transactions.length}`)
				renderKeys(keys)
				renderTransactions(transactions)
				scheduleTableScrollHintUpdate()
			} catch (error) {
				setState(error.message || 'Ошибка загрузки', true)
				renderNoKeysRow('Не удалось загрузить ключи')
				renderNoTransactionsRow('Не удалось загрузить транзакции')
				detailTelegramId.textContent = '—'
				detailUsername.textContent = '—'
				detailStatus.textContent = '—'
				detailStatus.className = ''
				detailTotalSpent.textContent = '—'
				detailRegistrationDate.textContent = '—'
				scheduleTableScrollHintUpdate()
			}
		}

		async function copyToClipboard(text) {
			if (!text) return false
			try {
				if (navigator.clipboard && navigator.clipboard.writeText) {
					await navigator.clipboard.writeText(text)
					return true
				}
			} catch (error) {
				// fall through to execCommand fallback
			}

			const helper = document.createElement('textarea')
			helper.value = text
			helper.setAttribute('readonly', '')
			helper.style.position = 'absolute'
			helper.style.left = '-9999px'
			document.body.appendChild(helper)
			helper.select()
			const copied = document.execCommand('copy')
			document.body.removeChild(helper)
			return copied
		}

		document.addEventListener('click', event => {
			const trigger = event.target.closest('.user-detail-trigger')
			if (!trigger) {
				return
			}

			event.preventDefault()
			const userId = trigger.dataset.userId
			if (!userId) {
				return
			}

			lastFocusedElement = trigger
			fetchUserDetails(userId)
		})

		modalOverlay.addEventListener('click', event => {
			if (event.target === modalOverlay) {
				closeModal()
			}
		})

		closeButton.addEventListener('click', closeModal)

		document.addEventListener('keydown', event => {
			if (event.key === 'Escape' && !modalOverlay.hidden) {
				closeModal()
			}
		})

			tableScrollSections.forEach(section => {
				const wrapper = section.querySelector('[data-scroll-wrapper]')
				if (!wrapper) {
					return
				}
				wrapper.addEventListener('scroll', scheduleTableScrollHintUpdate, { passive: true })

				// Enable natural horizontal scrolling with mouse wheel and drag gesture.
				wrapper.addEventListener(
					'wheel',
					event => {
						const hasOverflow = wrapper.scrollWidth - wrapper.clientWidth > 1
						if (!hasOverflow) {
							return
						}

						const horizontalDelta =
							Math.abs(event.deltaX) > Math.abs(event.deltaY)
								? event.deltaX
								: event.deltaY
						if (horizontalDelta === 0) {
							return
						}

						const atStart = wrapper.scrollLeft <= 0
						const atEnd =
							wrapper.scrollLeft + wrapper.clientWidth >= wrapper.scrollWidth - 1
						if ((horizontalDelta < 0 && atStart) || (horizontalDelta > 0 && atEnd)) {
							return
						}

						wrapper.scrollLeft += horizontalDelta
						event.preventDefault()
						scheduleTableScrollHintUpdate()
					},
					{ passive: false }
				)

				let isDragging = false
				let dragStartX = 0
				let dragStartScrollLeft = 0

				wrapper.addEventListener('pointerdown', event => {
					if (event.button !== 0) {
						return
					}
					if (
						event.target.closest(
							'button, a, input, select, textarea, label, [role="button"]'
						)
					) {
						return
					}
					if (wrapper.scrollWidth - wrapper.clientWidth <= 1) {
						return
					}

					isDragging = true
					dragStartX = event.clientX
					dragStartScrollLeft = wrapper.scrollLeft
					wrapper.classList.add('is-dragging')
					wrapper.setPointerCapture(event.pointerId)
					event.preventDefault()
				})

				wrapper.addEventListener('pointermove', event => {
					if (!isDragging) {
						return
					}
					const deltaX = event.clientX - dragStartX
					wrapper.scrollLeft = dragStartScrollLeft - deltaX
					scheduleTableScrollHintUpdate()
				})

				const stopDragging = event => {
					if (!isDragging) {
						return
					}
					isDragging = false
					wrapper.classList.remove('is-dragging')
					if (typeof event.pointerId === 'number' && wrapper.hasPointerCapture(event.pointerId)) {
						wrapper.releasePointerCapture(event.pointerId)
					}
				}

				wrapper.addEventListener('pointerup', stopDragging)
				wrapper.addEventListener('pointercancel', stopDragging)
				wrapper.addEventListener('lostpointercapture', () => {
					isDragging = false
					wrapper.classList.remove('is-dragging')
				})
			})

		window.addEventListener('resize', scheduleTableScrollHintUpdate)

		keysBody.addEventListener('click', async event => {
			const copyButton = event.target.closest('.user-detail-copy')
			if (!copyButton) {
				return
			}

			const value = copyButton.dataset.key || ''
			const copied = await copyToClipboard(value)
			if (!copied) {
				return
			}

			const originalText = copyButton.textContent
			copyButton.textContent = 'Скопировано'
			copyButton.disabled = true
			setTimeout(() => {
				copyButton.textContent = originalText
				copyButton.disabled = false
			}, 1400)
		})

		scheduleTableScrollHintUpdate()
	}

	function toggleChatwootFields() {
		var enabled = document.getElementById('chatwoot_enabled')
		if (!enabled) return
		var cw = document.getElementById('chatwootFields')
		var fb = document.getElementById('forumBotFields')
		if (cw) cw.style.display = enabled.checked ? '' : 'none'
		if (fb) fb.style.display = enabled.checked ? 'none' : ''
	}

	window.toggleChatwootFields = toggleChatwootFields

	function initializeWhitelistBalanceLookup() {
		const button = document.querySelector('[data-whitelist-balance-button]')
		const panel = document.querySelector('[data-whitelist-balance-panel]')
		const form = document.querySelector('.users-filters-form')
		const searchInput = form?.querySelector('input[name="q"]')
		if (!button || !panel || !form || !searchInput) return

		const escapeHtml = value =>
			String(value ?? '').replace(/[&<>"']/g, char => ({
				'&': '&amp;',
				'<': '&lt;',
				'>': '&gt;',
				'"': '&quot;',
				"'": '&#39;',
			})[char])

		const setPanelState = (html, state = 'info') => {
			panel.hidden = false
			panel.className = `users-whitelist-balance-panel users-whitelist-balance-panel--${state}`
			panel.innerHTML = html
		}

		const renderMessage = (message, state = 'info') => {
			setPanelState(`<div class="users-whitelist-balance-message">${escapeHtml(message)}</div>`, state)
		}

		const copyText = async value => {
			const text = String(value || '').trim()
			if (!text) return false
			if (navigator.clipboard?.writeText) {
				await navigator.clipboard.writeText(text)
				return true
			}
			const textarea = document.createElement('textarea')
			textarea.value = text
			textarea.setAttribute('readonly', '')
			textarea.style.position = 'fixed'
			textarea.style.left = '-9999px'
			document.body.appendChild(textarea)
			textarea.select()
			let copied = false
			try {
				copied = document.execCommand('copy')
			} finally {
				textarea.remove()
			}
			return copied
		}

		panel.addEventListener('click', async event => {
			const copyButton = event.target.closest('[data-copy-subscription]')
			if (!copyButton) return
			const subscriptionUrl = copyButton.dataset.copySubscription || ''
			const originalText = copyButton.textContent
			copyButton.disabled = true
			try {
				const copied = await copyText(subscriptionUrl)
				copyButton.textContent = copied ? 'Скопировано' : 'Не скопировано'
			} catch (error) {
				copyButton.textContent = 'Ошибка'
			} finally {
				setTimeout(() => {
					copyButton.textContent = originalText
					copyButton.disabled = false
				}, 1400)
			}
		})

		button.addEventListener('click', async () => {
			const query = (searchInput.value || '').trim()
			if (!query) {
				renderMessage('Введите Telegram ID или username в поиск.', 'warning')
				searchInput.focus()
				return
			}

			const originalText = button.textContent
			button.disabled = true
			button.textContent = 'Проверяю...'
			renderMessage('Проверяю остатки WHITELIST...', 'loading')

			try {
				const endpoint = button.dataset.endpoint || '/api/users/whitelist-balance'
				const url = new URL(endpoint, window.location.origin)
				url.searchParams.set('q', query)
				const response = await fetch(url.toString(), {
					headers: {Accept: 'application/json'},
				})
				const contentType = response.headers.get('content-type') || ''
				const data = contentType.includes('application/json')
					? await response.json()
					: {ok: false, error: 'Админка вернула не JSON. Обновите страницу и войдите снова.'}
				if (!response.ok || !data.ok) {
					renderMessage(data.error || 'Не удалось получить остатки по вайтлистам.', 'warning')
					return
				}

				const username = data.user?.username ? `@${data.user.username}` : ''
				const userLabel = [username, data.user?.telegram_id].filter(Boolean).join(' / ')
				const items = Array.isArray(data.items) ? data.items : []
				if (!items.length) {
					renderMessage(`У клиента ${userLabel || query} нет активных WHITELIST-лимитов в кэше.`, 'warning')
					return
				}

				const rows = items.map(item => `
					<div class="users-whitelist-balance-item ${item.exhausted ? 'is-exhausted' : ''}">
						<div class="users-whitelist-balance-item-head">
							<strong>${escapeHtml(item.server || 'WHITELIST')}</strong>
							<span>${escapeHtml(item.status || '')}</span>
						</div>
						<div class="users-whitelist-balance-grid">
							<span>Остаток</span><strong>${escapeHtml(item.remaining_label || '0 GiB')}</strong>
							<span>Использовано</span><strong>${escapeHtml(item.used_label || '0 GiB')} из ${escapeHtml(item.total_label || '0 GiB')}</strong>
							<span>Сброс</span><strong>${escapeHtml(item.reset_label || 'не запланирован')}</strong>
							<span>Подписка</span>
							<div class="users-whitelist-subscription-cell">
								<code>${escapeHtml(item.subscription_url || '')}</code>
								<button
									type="button"
									class="button button-small button-secondary users-whitelist-copy-button"
									data-copy-subscription="${escapeHtml(item.subscription_url || '')}"
									${item.subscription_url ? '' : 'disabled'}
								>
									Скопировать
								</button>
							</div>
							${item.updated_label ? `<span>Кэш</span><strong>${escapeHtml(item.updated_label)}</strong>` : ''}
						</div>
					</div>
				`).join('')

				setPanelState(`
					<div class="users-whitelist-balance-title">
						<span>Остаток по вайтлистам</span>
						<strong>${escapeHtml(userLabel || query)}</strong>
					</div>
					<div class="users-whitelist-balance-list">${rows}</div>
				`, 'success')
			} catch (error) {
				renderMessage(`Ошибка проверки: ${error.message}`, 'warning')
			} finally {
				button.disabled = false
				button.textContent = originalText
			}
		})
	}

	initializePasswordToggles()
	setupBotControlForms()
	setupConfirmationForms()
	setupFlashAutoClose()
	initializeDashboardCharts()
	initializeDashboardRangeModal()
	initializeDashboardTransactionsPagination()
	initializeLiveDataSync()
	initializeUserDetailsModal()
	initializeWhitelistBalanceLookup()
	initializeMigrationPage()
	initializeIssueByIdPage()
})

/* ============================================================
   Migration page
   ============================================================ */
function _migGetParams() {
	return {
		subscription_tier: (document.getElementById('mig-subscription-tier')?.value || 'super').trim(),
		bonus_days: parseInt(document.getElementById('mig-gift-days')?.value || '0', 10),
		incident_id: document.getElementById('mig-incident-id')?.value || '',
	}
}

let _migrationPollTimer = null
let _marketingPollTimer = null
let _mainpanelInboundPollTimer = null
let _issueActionInFlight = false
let _issueModalLastActiveElement = null
let _issuePendingRequest = null
const _ISSUE_REQUEST_STORAGE_KEY = 'pepelbot.issue-by-id.pending-request'
const _ISSUE_REQUEST_TTL_MS = 10 * 60 * 1000

function _issueCreateRequestId() {
	if (window.crypto?.randomUUID) return window.crypto.randomUUID()
	return `${Date.now()}-${Math.random().toString(36).slice(2)}-${Math.random().toString(36).slice(2)}`
}

function _issueReadPendingRequest() {
	if (_issuePendingRequest) return _issuePendingRequest
	try {
		_issuePendingRequest = JSON.parse(window.sessionStorage.getItem(_ISSUE_REQUEST_STORAGE_KEY) || 'null')
	} catch (_error) {
		_issuePendingRequest = null
	}
	return _issuePendingRequest
}

function _issueRequestIdForPayload(payload) {
	const fingerprint = JSON.stringify(payload)
	const pending = _issueReadPendingRequest()
	if (
		pending?.requestId
		&& pending.fingerprint === fingerprint
		&& Date.now() - Number(pending.createdAt || 0) <= _ISSUE_REQUEST_TTL_MS
	) {
		return pending.requestId
	}
	_issuePendingRequest = {
		requestId: _issueCreateRequestId(),
		fingerprint,
		createdAt: Date.now(),
	}
	try {
		window.sessionStorage.setItem(_ISSUE_REQUEST_STORAGE_KEY, JSON.stringify(_issuePendingRequest))
	} catch (_error) {
		// The in-memory token still protects retries while this page stays open.
	}
	return _issuePendingRequest.requestId
}

function _issueCompleteRequest(requestId) {
	if (_issuePendingRequest?.requestId === requestId) _issuePendingRequest = null
	try {
		const stored = JSON.parse(window.sessionStorage.getItem(_ISSUE_REQUEST_STORAGE_KEY) || 'null')
		if (stored?.requestId === requestId) window.sessionStorage.removeItem(_ISSUE_REQUEST_STORAGE_KEY)
	} catch (_error) {
		// There may be no session storage in hardened browser modes.
	}
}

function initializeMigrationPage() {
	// Reserved for migration-page specific initializers.
}

function initializeIssueByIdPage() {
	const modeSelect = document.getElementById('issue-mode')
	const hostSelect = document.getElementById('issue-host')
	const tierSelect = document.getElementById('issue-subscription-tier')
	const daysInput = document.getElementById('issue-duration-days')
	const modalOverlay = document.getElementById('issue-keys-modal')
	if (!modeSelect || !hostSelect || !daysInput || !modalOverlay) return
	if (!modeSelect.value) modeSelect.value = 'new'
	_issueUpdateExecuteButtonLabel()
	_issueUpdateTierHint()

	modeSelect.addEventListener('change', () => {
		_issueUpdateExecuteButtonLabel()
	})
	if (tierSelect) {
		tierSelect.addEventListener('change', _issueUpdateTierHint)
	}
	hostSelect.addEventListener('change', _issueUpdateTierHint)

	modalOverlay.addEventListener('click', (event) => {
		if (event.target === modalOverlay) {
			issueByIdCloseKeysModal()
		}
	})

	document.addEventListener('keydown', (event) => {
		if (modalOverlay.hidden) return
		if (event.key === 'Escape') {
			issueByIdCloseKeysModal()
			return
		}
		if (event.key !== 'Tab') return

		const focusable = _issueGetFocusableElements(modalOverlay)
		if (!focusable.length) {
			event.preventDefault()
			return
		}

		const first = focusable[0]
		const last = focusable[focusable.length - 1]
		if (event.shiftKey && document.activeElement === first) {
			event.preventDefault()
			last.focus()
			return
		}
		if (!event.shiftKey && document.activeElement === last) {
			event.preventDefault()
			first.focus()
		}
	})
}

function _issueGetFocusableElements(modalOverlay) {
	const dialog = modalOverlay?.querySelector('.issue-keys-modal') || modalOverlay
	if (!dialog) return []
	const candidates = dialog.querySelectorAll(
		'button:not([disabled]), [href], input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])',
	)
	return Array.from(candidates).filter((element) => {
		const style = window.getComputedStyle(element)
		return style.display !== 'none' && style.visibility !== 'hidden'
	})
}

function _issueUpdateExecuteButtonLabel() {
	const btn = document.getElementById('issue-btn-execute')
	const mode = document.getElementById('issue-mode')?.value || 'new'
	if (!btn || _issueActionInFlight) return
	btn.textContent = mode === 'replace' ? 'Перевыпустить ключ' : 'Выдать ключ'
}

function _issueUpdateTierHint() {
	const hint = document.getElementById('issue-tier-hint')
	const tier = document.getElementById('issue-subscription-tier')?.value || 'super'
	const hostSelect = document.getElementById('issue-host')
	const selectedHost = hostSelect?.selectedOptions?.[0] || null
	if (!hint) return
	const tierText = tier === 'standard'
		? 'Стандарт: выдача только по обычным inbound; WHITELIST не создаётся.'
		: 'Супер: выдача по всем inbound; WHITELIST получает лимит 40 GiB.'
	if (!selectedHost || !selectedHost.value) {
		hint.textContent = tierText
		return
	}
	const subscriptionText = selectedHost.dataset.hasSubscriptionUrl === 'true'
		? 'Клиент получит один URL подписки выбранного хоста.'
		: 'У выбранного хоста не настроен URL подписки; будет показана прямая ссылка подключения.'
	hint.textContent = `${tierText} ${subscriptionText}`
}

function _issueSetActionLock(locked) {
	_issueActionInFlight = locked
	const executeBtn = document.getElementById('issue-btn-execute')
	if (executeBtn) executeBtn.disabled = locked
	document.querySelectorAll('.issue-modal-action').forEach((button) => {
		button.disabled = locked
	})
}

function _migFormatDate(value) {
	if (!value) return '—'
	const date = new Date(value)
	if (Number.isNaN(date.getTime())) return value
	const dd = String(date.getDate()).padStart(2, '0')
	const mm = String(date.getMonth() + 1).padStart(2, '0')
	const yyyy = date.getFullYear()
	const hh = String(date.getHours()).padStart(2, '0')
	const min = String(date.getMinutes()).padStart(2, '0')
	return `${dd}.${mm}.${yyyy} ${hh}:${min}`
}

function _migAppendCell(tr, value) {
	const td = document.createElement('td')
	td.textContent = value
	tr.appendChild(td)
}

function _migShowStatus(text, phase) {
	const el = document.getElementById('mig-status')
	const textEl = document.getElementById('mig-status-text')
	el.hidden = false
	textEl.textContent = text
}

function _migUpdateSummary(data) {
	const wrap = document.getElementById('mig-summary')
	wrap.hidden = false
	document.getElementById('mig-sum-total').textContent = data.found || data.total || 0
	document.getElementById('mig-sum-will').textContent = data.will_migrate || 0
	document.getElementById('mig-sum-migrated').textContent = data.migrated || 0
	document.getElementById('mig-sum-skipped').textContent = data.skipped || 0
	document.getElementById('mig-sum-review').textContent = data.needs_review || 0
	document.getElementById('mig-sum-errors').textContent = data.errors || 0
}

function _migRenderTable(details, isDryRun) {
	const tbody = document.getElementById('mig-table-body')
	const wrap = document.getElementById('mig-table-wrap')
	const empty = document.getElementById('mig-empty')
	tbody.innerHTML = ''

	if (!details || details.length === 0) {
		wrap.hidden = true
		empty.hidden = false
		empty.textContent = 'Нет ключей для миграции.'
		return
	}

	empty.hidden = true
	wrap.hidden = false

	details.forEach(d => {
		const tr = document.createElement('tr')
		const statusClass = d.already_migrated ? 'mig-status-skipped'
			: d.status === 'migrated' ? 'mig-status-migrated'
			: d.status === 'error' ? 'mig-status-error'
			: d.status === 'needs_review' ? 'mig-status-error'
			: 'mig-status-dry'
		const statusText = d.already_migrated ? 'уже мигрирован'
			: d.status === 'migrated' ? 'OK'
			: d.status === 'error' ? (d.error || 'ошибка')
			: d.status === 'will_migrate' ? 'к миграции'
			: d.status === 'already_migrated' ? 'уже мигрирован'
			: d.status === 'skipped_expired' ? 'expired: skip'
			: d.status === 'skipped_wrong_tier' ? 'другой тариф'
			: d.status === 'needs_review' ? 'ручная проверка'
			: d.status === 'skipped_claimed' ? 'уже выполняется'
			: d.status ? d.status
			: isDryRun ? 'dry-run' : '—'

		_migAppendCell(tr, d.user_id != null ? String(d.user_id) : '—')
		_migAppendCell(tr, d.old_host || '—')
		_migAppendCell(tr, d.key_email || d.key_id || '—')
		_migAppendCell(tr, d.resolved_tier || '—')
		_migAppendCell(tr, d.old_expiry ? _migFormatDate(d.old_expiry) : '—')
		_migAppendCell(tr, d.bonus_days != null ? `+${d.bonus_days}д` : '—')
		_migAppendCell(
			tr,
			d.new_expiry ? _migFormatDate(d.new_expiry) : '—',
		)
		const statusTd = document.createElement('td')
		const statusSpan = document.createElement('span')
		statusSpan.className = statusClass
		statusSpan.textContent = statusText
		statusTd.appendChild(statusSpan)
		tr.appendChild(statusTd)
		tbody.appendChild(tr)
	})
}

async function migrationPreview() {
	const params = _migGetParams()

	const btn = document.getElementById('mig-btn-preview')
	btn.disabled = true
	btn.textContent = 'Загрузка...'
	_migShowStatus('Сбор dry-run по MainPanel...')

	try {
		const resp = await fetch('/api/migration/mainpanel/preview', {
			method: 'POST',
			headers: {'Content-Type': 'application/json'},
			body: JSON.stringify(params),
		})
		const data = await resp.json()
		if (!resp.ok) {
			alert(data.error || 'Ошибка')
			return
		}

		_migUpdateSummary(data)
		_migRenderTable(data.details, true)
		_migShowStatus(`Найдено ${data.found || data.total || 0}; к миграции ${data.will_migrate || 0}; ручная проверка ${data.needs_review || 0}.`)

		// Enable execute button
		const execBtn = document.getElementById('mig-btn-execute')
		execBtn.disabled = (data.will_migrate || 0) === 0
	} catch (err) {
		alert('Ошибка: ' + err.message)
	} finally {
		btn.disabled = false
		btn.textContent = 'Превью (dry-run)'
	}
}

async function migrationExecute() {
	const params = _migGetParams()
	if (!params.incident_id) {
		alert('Укажите ID миграции')
		return
	}
	const tierLabel = params.subscription_tier === 'standard' ? 'Стандарт' : 'Супер'
	if (!confirm(`Выполнить миграцию на MainPanel?\nКогорта: ${tierLabel}.\n\nЭто создаст реальные ключи на MainPanel. Уведомления пользователям не отправляются автоматически.`)) {
		return
	}

	const btnExec = document.getElementById('mig-btn-execute')
	const btnPreview = document.getElementById('mig-btn-preview')
	btnExec.disabled = true
	btnPreview.disabled = true
	btnExec.textContent = 'Выполняется...'

	const progressWrap = document.getElementById('mig-progress-bar-wrap')
	const progressBar = document.getElementById('mig-progress-bar')
	const progressDetail = document.getElementById('mig-progress-detail')
	progressWrap.hidden = false
	progressBar.style.width = '0%'
	progressBar.style.background = 'var(--success-color)'
	progressDetail.textContent = '0/0 | OK: 0 | Ошибки: 0'

	try {
		const resp = await fetch('/api/migration/mainpanel/execute', {
			method: 'POST',
			headers: {'Content-Type': 'application/json'},
			body: JSON.stringify(params),
		})
		const data = await resp.json()
		if (!resp.ok) {
			throw new Error(data.error || 'Ошибка')
		}

		const jobId = data.job_id
		if (_migrationPollTimer) {
			clearInterval(_migrationPollTimer)
		}
		// Poll for status
		_migrationPollTimer = setInterval(async () => {
			try {
				const sr = await fetch('/api/migration/mainpanel/status/' + jobId)
				if (!sr.ok) {
					throw new Error('Не удалось получить статус миграции')
				}
				const job = await sr.json()

				const total = job.found || job.total || 1
				const done = (job.details || []).length
				const pct = Math.round((done / total) * 100)
				progressBar.style.width = pct + '%'

				const phaseText = job.phase === 'migrating' ? 'Миграция ключей...'
					: job.phase === 'done' ? 'Завершено'
					: job.phase === 'error' ? 'Ошибка: ' + (job.error || '')
					: job.phase
				_migShowStatus(phaseText)
				progressDetail.textContent = `${done}/${total} | OK: ${job.migrated || 0} | Ошибки: ${job.errors || 0}`

				_migUpdateSummary(job)

				if (job.status === 'completed' || job.status === 'failed') {
					clearInterval(_migrationPollTimer)
					_migrationPollTimer = null
					progressBar.style.width = '100%'
					if (job.status === 'completed') {
						progressBar.style.background = 'var(--success-color)'
						_migShowStatus(`Миграция завершена. Мигрировано: ${job.migrated}, ручная проверка: ${job.needs_review || 0}`)
					} else {
						progressBar.style.background = 'var(--danger-color)'
						_migShowStatus(`Ошибка миграции: ${job.error || 'подробности в логах'}`)
					}
					btnPreview.disabled = false
					btnExec.disabled = false
					btnExec.textContent = 'Выполнить миграцию'
					// Render final table
					_migRenderTable(job.details, false)
				}
			} catch (e) {
				console.error('Poll error:', e)
			}
		}, 1500)

	} catch (err) {
		alert('Ошибка: ' + err.message)
		btnExec.disabled = false
		btnPreview.disabled = false
		btnExec.textContent = 'Выполнить миграцию'
	}
}

function _mkGetParams() {
	return {
		segment: (document.getElementById('mk-segment')?.value || 'all').trim(),
		target_host: (document.getElementById('mk-target-host')?.value || '').trim(),
		subscription_tier: (document.getElementById('mk-subscription-tier')?.value || 'super').trim(),
		gift_days: parseInt(document.getElementById('mk-gift-days')?.value || '0', 10),
		incident_id: (document.getElementById('mk-incident-id')?.value || '').trim(),
		notification_text: document.getElementById('mk-notification-text')?.value || '',
		send_notify: !!document.getElementById('mk-send-notify')?.checked,
		keep_old_keys: !!document.getElementById('mk-keep-old-keys')?.checked,
		bot_source: (document.getElementById('mk-bot-source')?.value || 'main').trim(),
	}
}

function _mkShowStatus(text, isError = false) {
	const wrap = document.getElementById('mk-status')
	const textEl = document.getElementById('mk-status-text')
	if (!wrap || !textEl) return
	wrap.hidden = false
	textEl.textContent = text
	textEl.classList.toggle('mig-status-error', !!isError)
}

function _mkUpdateSummary(data) {
	const wrap = document.getElementById('mk-summary')
	if (!wrap) return
	wrap.hidden = false
	document.getElementById('mk-sum-total').textContent = data.total || 0
	document.getElementById('mk-sum-issued').textContent = data.issued || 0
	const deletedEl = document.getElementById('mk-sum-deleted')
	if (deletedEl) deletedEl.textContent = data.deleted || 0
	document.getElementById('mk-sum-skipped').textContent = data.skipped || 0
	document.getElementById('mk-sum-errors').textContent = data.errors || 0
	const deleteErrorsEl = document.getElementById('mk-sum-delete-errors')
	if (deleteErrorsEl) deleteErrorsEl.textContent = data.delete_errors || 0
	document.getElementById('mk-sum-sent').textContent = data.sent || 0
}

function _mkRenderTable(details, isDryRun) {
	const tbody = document.getElementById('mk-table-body')
	const wrap = document.getElementById('mk-table-wrap')
	const empty = document.getElementById('mk-empty')
	if (!tbody || !wrap || !empty) return
	tbody.innerHTML = ''

	if (!details || details.length === 0) {
		wrap.hidden = true
		empty.hidden = false
		empty.textContent = 'Нет пользователей для маркетинговой выдачи.'
		return
	}

	empty.hidden = true
	wrap.hidden = false

	details.forEach((d) => {
		const tr = document.createElement('tr')
		const statusText = d.already_processed
			? 'уже выдано'
			: d.status === 'issued_deleted'
			? 'выдано, старый удалён'
			: d.status === 'issued_delete_failed'
			? `выдано, удаление не удалось: ${d.delete_error || 'ошибка'}`
			: d.status === 'issued_keep_old'
			? 'выдано, старый оставлен'
			: d.status === 'issued'
			? 'выдано'
			: d.status === 'error'
			? (d.error || 'ошибка')
			: d.status === 'skipped'
			? 'пропущен'
			: isDryRun && d.old_key_action === 'delete_after_issue'
			? 'выдать + удалить старый'
			: isDryRun && d.old_key_action === 'keep'
			? 'выдать, старый оставить'
			: isDryRun
			? 'dry-run'
			: (d.status || '—')
		const statusClass = d.already_processed
			? 'mig-status-skipped'
			: d.status === 'issued_delete_failed'
			? 'mig-status-error'
			: ['issued', 'issued_deleted', 'issued_keep_old'].includes(d.status)
			? 'mig-status-migrated'
			: d.status === 'error'
			? 'mig-status-error'
			: 'mig-status-dry'

		_migAppendCell(tr, d.user_id != null ? String(d.user_id) : '—')
		_migAppendCell(tr, d.source_host || '—')
		_migAppendCell(tr, d.source_key_email || '—')
		_migAppendCell(tr, d.remaining_days != null ? `${d.remaining_days}д` : '—')
		_migAppendCell(tr, d.gift_days != null ? `+${d.gift_days}д` : '—')
		_migAppendCell(tr, d.new_expiry ? _migFormatDate(d.new_expiry) : '—')

		const statusTd = document.createElement('td')
		const statusSpan = document.createElement('span')
		statusSpan.className = statusClass
		statusSpan.textContent = statusText
		statusTd.appendChild(statusSpan)
		tr.appendChild(statusTd)
		tbody.appendChild(tr)
	})
}

async function marketingPreview() {
	const params = _mkGetParams()
	if (!params.target_host) {
		alert('Выберите хост для выдачи')
		return
	}
	const btn = document.getElementById('mk-btn-preview')
	const execBtn = document.getElementById('mk-btn-execute')
	btn.disabled = true
	btn.textContent = 'Загрузка...'
	_mkShowStatus('Собираю пользователей для выдачи...')

	try {
		const resp = await fetch('/api/migration/marketing/preview', {
			method: 'POST',
			headers: {'Content-Type': 'application/json'},
			body: JSON.stringify(params),
		})
		const data = await resp.json()
		if (!resp.ok) {
			throw new Error(data.error || 'Ошибка превью')
		}
		_mkUpdateSummary({
			total: data.total,
			issued: 0,
			deleted: 0,
			skipped: data.skipped,
			errors: 0,
			delete_errors: 0,
			sent: 0,
		})
		_mkRenderTable(data.details, true)
		const oldKeysMode = data.keep_old_keys
			? 'старые ключи останутся'
			: 'старые ключи будут удалены после выдачи'
		_mkShowStatus(`Найдено ${data.total} пользователей (пропущено: ${data.skipped}); ${oldKeysMode}`)
		execBtn.disabled = data.total === 0 || data.total === data.skipped
	} catch (error) {
		alert(`Ошибка: ${error.message}`)
	} finally {
		btn.disabled = false
		btn.textContent = 'Превью (dry-run)'
	}
}

async function marketingExecute() {
	const params = _mkGetParams()
	if (!params.incident_id) {
		alert('Укажите ID кампании')
		return
	}
	if (!params.target_host) {
		alert('Выберите хост для выдачи')
		return
	}
	const oldKeysMode = params.keep_old_keys
		? 'старые ключи останутся'
		: 'старые ключи будут удалены после успешной выдачи'
	const tierLabel = params.subscription_tier === 'standard' ? 'Стандарт' : 'Супер'
	if (!confirm(`Запустить маркетинговую выдачу на хост ${params.target_host}? Тариф: ${tierLabel}; ${oldKeysMode}.`)) {
		return
	}

	const btnPreview = document.getElementById('mk-btn-preview')
	const btnExecute = document.getElementById('mk-btn-execute')
	const progressWrap = document.getElementById('mk-progress-wrap')
	const progressBar = document.getElementById('mk-progress-bar')
	const progressDetail = document.getElementById('mk-progress-detail')

	btnPreview.disabled = true
	btnExecute.disabled = true
	btnExecute.textContent = 'Выполняется...'
	if (progressWrap && progressBar && progressDetail) {
		progressWrap.hidden = false
		progressBar.style.width = '0%'
		progressBar.style.background = 'var(--success-color)'
		progressDetail.textContent = '0/0 | Выдано: 0 | Удалено: 0 | Ошибки: 0 | Отправлено: 0'
	}
	_mkShowStatus('Запуск кампании...')

	try {
		const resp = await fetch('/api/migration/marketing/execute', {
			method: 'POST',
			headers: {'Content-Type': 'application/json'},
			body: JSON.stringify(params),
		})
		const data = await resp.json()
		if (!resp.ok) {
			throw new Error(data.error || 'Ошибка запуска')
		}

		const jobId = data.job_id
		if (_marketingPollTimer) clearInterval(_marketingPollTimer)
		_marketingPollTimer = setInterval(async () => {
			try {
				const sr = await fetch(`/api/migration/marketing/status/${jobId}`)
				if (!sr.ok) throw new Error('Не удалось получить статус кампании')
				const job = await sr.json()

				const total = job.total || 1
				const done = (job.issued || 0) + (job.skipped || 0) + (job.errors || 0)
				const pct = Math.round((done / total) * 100)
				if (progressBar) progressBar.style.width = `${pct}%`
				if (progressDetail) {
					progressDetail.textContent = `${done}/${total} | Выдано: ${job.issued || 0} | Удалено: ${job.deleted || 0} | Ошибки: ${job.errors || 0} | Ошибки удаления: ${job.delete_errors || 0} | Отправлено: ${job.sent || 0}`
				}

				const phaseText = job.phase === 'issuing'
					? 'Выдача ключей...'
					: job.phase === 'notifying'
					? 'Отправка уведомлений...'
					: job.phase === 'done'
					? 'Кампания завершена'
					: job.phase === 'error'
					? `Ошибка: ${job.error || 'подробности в логах'}`
					: (job.phase || 'Выполняется...')
				_mkShowStatus(phaseText, job.status === 'failed')

				_mkUpdateSummary(job)
				_mkRenderTable(job.details, false)

				if (job.status === 'completed' || job.status === 'failed') {
					clearInterval(_marketingPollTimer)
					_marketingPollTimer = null
					if (progressBar) progressBar.style.width = '100%'
					if (job.status === 'failed' && progressBar) {
						progressBar.style.background = 'var(--danger-color)'
					}
					btnPreview.disabled = false
					btnExecute.disabled = false
					btnExecute.textContent = 'Запустить выдачу'
				}
			} catch (error) {
				console.error('Marketing poll error:', error)
			}
		}, 1500)
	} catch (error) {
		alert(`Ошибка: ${error.message}`)
		btnPreview.disabled = false
		btnExecute.disabled = false
		btnExecute.textContent = 'Запустить выдачу'
	}
}

function _inbGetParams() {
	return {
		host_name: (document.getElementById('inb-host')?.value || '').trim(),
		batch_limit: parseInt(document.getElementById('inb-batch-limit')?.value || '100', 10),
	}
}

function _inbShowStatus(text, isError = false) {
	const wrap = document.getElementById('inb-status')
	const textEl = document.getElementById('inb-status-text')
	if (!wrap || !textEl) return
	wrap.hidden = false
	textEl.textContent = text
	textEl.classList.toggle('mig-status-error', !!isError)
}

function _inbUpdateSummary(data) {
	const wrap = document.getElementById('inb-summary')
	if (!wrap) return
	wrap.hidden = false
	document.getElementById('inb-sum-total').textContent = data.total || data.processed || 0
	document.getElementById('inb-sum-updated').textContent = data.updated || 0
	document.getElementById('inb-sum-skipped').textContent = data.skipped || 0
	document.getElementById('inb-sum-review').textContent = data.needs_review || 0
	document.getElementById('inb-sum-failed').textContent = data.failed || 0
	document.getElementById('inb-sum-new').textContent = data.new_inbounds || 0
}

function _inbRenderTable(details) {
	const tbody = document.getElementById('inb-table-body')
	const wrap = document.getElementById('inb-table-wrap')
	const empty = document.getElementById('inb-empty')
	if (!tbody || !wrap || !empty) return
	tbody.innerHTML = ''
	if (!details || details.length === 0) {
		wrap.hidden = true
		empty.hidden = false
		empty.textContent = 'Нет действий для sync.'
		return
	}

	empty.hidden = true
	wrap.hidden = false
	details.forEach((d) => {
		const tr = document.createElement('tr')
		const statusClass = ['updated', 'would_update'].includes(d.status)
			? 'mig-status-migrated'
			: d.status === 'failed'
			? 'mig-status-error'
			: d.status === 'needs_review'
			? 'mig-status-skipped'
			: 'mig-status-dry'
		const statusText = d.status === 'would_update'
			? 'добавить'
			: d.status === 'updated'
			? 'добавлено'
			: d.status === 'skipped' && d.action === 'already_synced'
			? 'уже есть'
			: d.status === 'skipped' && d.action === 'tier_excluded'
			? 'не входит в тариф'
			: d.status === 'needs_review'
			? `проверить: ${d.error || 'данные'}`
			: d.status === 'failed'
			? `ошибка: ${d.error || 'panel'}`
			: (d.status || '—')

		_migAppendCell(tr, d.user_id != null ? String(d.user_id) : '—')
		_migAppendCell(tr, d.email || '—')
		_migAppendCell(tr, d.inbound_label || (d.inbound_id != null ? String(d.inbound_id) : '—'))
		_migAppendCell(tr, d.classification || '—')
		_migAppendCell(tr, d.tier || '—')
		const statusTd = document.createElement('td')
		const statusSpan = document.createElement('span')
		statusSpan.className = statusClass
		statusSpan.textContent = statusText
		statusTd.appendChild(statusSpan)
		tr.appendChild(statusTd)
		tbody.appendChild(tr)
	})
}

async function mainpanelInboundPreview() {
	const params = _inbGetParams()
	const btn = document.getElementById('inb-btn-preview')
	const execBtn = document.getElementById('inb-btn-execute')
	btn.disabled = true
	btn.textContent = 'Загрузка...'
	_inbShowStatus('Собираю dry-run...')
	try {
		const resp = await fetch('/api/migration/mainpanel-inbounds/preview', {
			method: 'POST',
			headers: {'Content-Type': 'application/json'},
			body: JSON.stringify(params),
		})
		const data = await resp.json()
		if (!resp.ok) throw new Error(data.error || 'Ошибка preview')
		_inbUpdateSummary(data)
		_inbRenderTable(data.details)
		_inbShowStatus(`Dry-run готов. К добавлению: ${data.updated || 0}; ручная проверка: ${data.needs_review || 0}.`)
		execBtn.disabled = (data.updated || 0) === 0
	} catch (error) {
		alert(`Ошибка: ${error.message}`)
	} finally {
		btn.disabled = false
		btn.textContent = 'Превью (dry-run)'
	}
}

async function mainpanelInboundExecute() {
	const params = _inbGetParams()
	if (!confirm('Запустить sync клиентов MainPanel по inbound-ам?')) return
	const btnPreview = document.getElementById('inb-btn-preview')
	const btnExecute = document.getElementById('inb-btn-execute')
	const progressWrap = document.getElementById('inb-progress-wrap')
	const progressBar = document.getElementById('inb-progress-bar')
	const progressDetail = document.getElementById('inb-progress-detail')
	btnPreview.disabled = true
	btnExecute.disabled = true
	btnExecute.textContent = 'Выполняется...'
	if (progressWrap && progressBar && progressDetail) {
		progressWrap.hidden = false
		progressBar.style.width = '0%'
		progressBar.style.background = 'var(--success-color)'
		progressDetail.textContent = '0/0 | Добавлено: 0 | Ошибки: 0'
	}
	_inbShowStatus('Запуск sync...')
	try {
		const resp = await fetch('/api/migration/mainpanel-inbounds/execute', {
			method: 'POST',
			headers: {'Content-Type': 'application/json'},
			body: JSON.stringify(params),
		})
		const data = await resp.json()
		if (!resp.ok) throw new Error(data.error || 'Ошибка запуска')
		const jobId = data.run_id || data.job_id
		if (_mainpanelInboundPollTimer) clearInterval(_mainpanelInboundPollTimer)
		_mainpanelInboundPollTimer = setInterval(async () => {
			try {
				const sr = await fetch(`/api/migration/mainpanel-inbounds/status/${jobId}`)
				if (!sr.ok) return
				const job = await sr.json()
				const total = job.total || job.processed || 1
				const done = job.processed || 0
				const pct = Math.min(100, Math.round((done / total) * 100))
				if (progressBar) progressBar.style.width = `${pct}%`
				if (progressDetail) {
					progressDetail.textContent = `${done}/${total} | Добавлено: ${job.updated || 0} | Пропущено: ${job.skipped || 0} | Ошибки: ${job.failed || 0}`
				}
				_inbUpdateSummary(job)
				_inbRenderTable(job.details)
				const finished = ['completed', 'completed_with_errors', 'failed'].includes(job.status)
				_inbShowStatus(
					finished ? `Sync завершен. Добавлено: ${job.updated || 0}; ошибок: ${job.failed || 0}.` : 'Sync выполняется...',
					job.status === 'failed',
				)
				if (finished) {
					clearInterval(_mainpanelInboundPollTimer)
					_mainpanelInboundPollTimer = null
					if (progressBar) progressBar.style.width = '100%'
					if (job.status === 'failed' && progressBar) progressBar.style.background = 'var(--danger-color)'
					btnPreview.disabled = false
					btnExecute.disabled = false
					btnExecute.textContent = 'Запустить sync'
				}
			} catch (error) {
				console.error('MainPanel inbound sync poll error:', error)
			}
		}, 1500)
	} catch (error) {
		alert(`Ошибка: ${error.message}`)
		btnPreview.disabled = false
		btnExecute.disabled = false
		btnExecute.textContent = 'Запустить sync'
	}
}

function _trialResetGetParams() {
	return {
		username: (document.getElementById('trial-reset-username')?.value || '').trim(),
		confirm: 'reset-user-trials',
	}
}

function _trialResetShowStatus(text, isError = false) {
	const wrap = document.getElementById('trial-reset-status')
	const textEl = document.getElementById('trial-reset-status-text')
	if (!wrap || !textEl) return
	wrap.hidden = false
	textEl.textContent = text
	textEl.classList.toggle('mig-status-error', !!isError)
}

function _trialResetSetResult(data) {
	const result = document.getElementById('trial-reset-result')
	const empty = document.getElementById('trial-reset-empty')
	if (!result || !empty) return
	result.hidden = false
	empty.hidden = true
	document.getElementById('trial-reset-result-telegram-id').textContent = data.telegram_id || '—'
	document.getElementById('trial-reset-result-username').textContent = data.username || '—'
	document.getElementById('trial-reset-result-standard').textContent = `${data.trial_used_before || 0} → ${data.trial_used_after || 0}`
	document.getElementById('trial-reset-result-super').textContent = `${data.trial_super_used_before || 0} → ${data.trial_super_used_after || 0}`
}

function _trialResetErrorMessage(data) {
	if (!data || !data.error) return 'Ошибка сброса trial'
	if (data.error === 'user_not_found') return 'Пользователь с таким username не найден.'
	if (data.error === 'ambiguous_username') return `Найдено несколько пользователей: ${data.details || 'проверьте username'}.`
	if (data.error === 'invalid_username') return 'Username может содержать только латиницу, цифры и подчёркивание.'
	if (data.error === 'confirmation_required') return 'Не передано подтверждение операции.'
	return data.error
}

async function trialResetExecute() {
	const params = _trialResetGetParams()
	const normalizedUsername = params.username.replace(/^@+/, '')
	if (!normalizedUsername) {
		_trialResetShowStatus('Введите username.', true)
		return
	}
	if (!/^[A-Za-z0-9_]{1,64}$/.test(normalizedUsername)) {
		_trialResetShowStatus('Username может содержать только латиницу, цифры и подчёркивание.', true)
		return
	}
	if (!confirm(`Сбросить обычный и Super trial для @${normalizedUsername}?`)) return

	const button = document.getElementById('trial-reset-btn-execute')
	if (button) {
		button.disabled = true
		button.textContent = 'Сбрасываю...'
	}
	_trialResetShowStatus('Сбрасываю trial-флаги...')

	try {
		const resp = await fetch('/api/migration/reset-trials', {
			method: 'POST',
			headers: {'Content-Type': 'application/json'},
			body: JSON.stringify({...params, username: normalizedUsername}),
		})
		const data = await resp.json()
		if (!resp.ok) {
			_trialResetShowStatus(_trialResetErrorMessage(data), true)
			return
		}
		_trialResetSetResult(data)
		_trialResetShowStatus(data.changed ? 'Trial-флаги сброшены.' : 'Trial-флаги уже были сброшены.')
	} catch (error) {
		_trialResetShowStatus(`Ошибка: ${error.message}`, true)
	} finally {
		if (button) {
			button.disabled = false
			button.textContent = 'Сбросить триалы'
		}
	}
}

		function switchMigrationTab(tabName) {
			const normalized = ['bulk', 'issue', 'marketing', 'inbounds', 'trial-reset'].includes(tabName) ? tabName : 'bulk'
		const panels = document.querySelectorAll('[data-migration-panel]')
		const buttons = document.querySelectorAll('[data-migration-tab]')
		if (!panels.length || !buttons.length) return

		panels.forEach((panel) => {
			const isActive = panel.dataset.migrationPanel === normalized
			panel.hidden = !isActive
			panel.classList.toggle('migration-tab-panel--active', isActive)
		})

		buttons.forEach((button) => {
			const isActive = button.dataset.migrationTab === normalized
			button.classList.toggle('migration-tab-button--active', isActive)
			button.setAttribute('aria-selected', isActive ? 'true' : 'false')
		})

		const url = new URL(window.location.href)
		if (normalized === 'bulk') {
			url.searchParams.delete('tab')
		} else {
			url.searchParams.set('tab', normalized)
		}
		window.history.replaceState({}, '', url.toString())
	}

	document.addEventListener('DOMContentLoaded', () => {
		const hasMigrationTabs = document.querySelector('[data-migration-tab]')
		if (!hasMigrationTabs) return
		const params = new URLSearchParams(window.location.search)
		switchMigrationTab(params.get('tab') || 'bulk')
		issueDurationSync()
	})

function issueDurationSync() {
	const unlimited = !!document.getElementById('issue-unlimited')?.checked
	;['issue-duration-days', 'issue-duration-months', 'issue-duration-years'].forEach((id) => {
		const field = document.getElementById(id)
		if (!field) return
		field.disabled = unlimited
	})
}

function _issueSetStatus(text, isError = false) {
	const wrap = document.getElementById('issue-status')
	const textEl = document.getElementById('issue-status-text')
	if (!wrap || !textEl) return
	wrap.hidden = false
	textEl.textContent = text
	textEl.classList.toggle('mig-status-error', isError)
}

function _issueSetModalStatus(text, isError = false) {
	const textEl = document.getElementById('issue-keys-modal-status')
	if (!textEl) return
	textEl.textContent = text
	textEl.classList.toggle('mig-status-error', isError)
}

function _issueReadDurationInputs() {
	const unlimited = !!document.getElementById('issue-unlimited')?.checked
	const days = parseInt(document.getElementById('issue-duration-days')?.value || '0', 10) || 0
	const months = parseInt(document.getElementById('issue-duration-months')?.value || '0', 10) || 0
	const years = parseInt(document.getElementById('issue-duration-years')?.value || '0', 10) || 0
	if (!unlimited && !(days || months || years)) {
		throw new Error('Укажите срок: дни, месяцы, годы или включите безлимит')
	}
	return { duration_days: days, duration_months: months, duration_years: years, unlimited: unlimited }
}

function _issueGetFormBasePayload() {
	const telegramId = (document.getElementById('issue-telegram-id')?.value || '').trim()
	const hostName = (document.getElementById('issue-host')?.value || '').trim()
	const subscriptionTier = (document.getElementById('issue-subscription-tier')?.value || 'super').trim()
	const botSource = (document.getElementById('issue-bot-source')?.value || 'reserve').trim()
	const sendMessage = !!document.getElementById('issue-send-message')?.checked
	const messageText = document.getElementById('issue-message-text')?.value || ''
	const targetKeyRaw = (document.getElementById('issue-target-key-id')?.value || '').trim()
	let targetKeyId = null
	if (targetKeyRaw) {
		targetKeyId = parseInt(targetKeyRaw, 10)
		if (!Number.isInteger(targetKeyId) || targetKeyId <= 0) {
			throw new Error('Некорректный выбранный ключ')
		}
	}
	if (!/^\d+$/.test(telegramId)) {
		throw new Error('Укажите корректный Telegram ID')
	}
	return {
		telegram_id: telegramId,
		host_name: hostName,
		subscription_tier: subscriptionTier,
		bot_source: botSource,
		send_message: sendMessage,
		message_text: messageText,
		target_key_id: targetKeyId,
	}
}

function _issueSetSelectedKeyHint(text) {
	const hint = document.getElementById('issue-selected-key-hint')
	if (!hint) return
	hint.textContent = text
}

function issueByIdResetTargetKey() {
	const hidden = document.getElementById('issue-target-key-id')
	if (hidden) hidden.value = ''
	_issueSetSelectedKeyHint('Ключ не выбран. В режиме замены будет выбран автоматически по хосту.')
}

function issueByIdSelectKeyForReplace(keyId, hostName, keyEmail = '') {
	const hidden = document.getElementById('issue-target-key-id')
	const hostSelect = document.getElementById('issue-host')
	const modeSelect = document.getElementById('issue-mode')
	if (hidden) hidden.value = String(keyId)
	if (modeSelect) modeSelect.value = 'replace'
	if (hostSelect && hostName) {
		const hasOption = Array.from(hostSelect.options).some((opt) => opt.value === hostName)
		if (!hasOption) {
			const option = document.createElement('option')
			option.value = hostName
			option.textContent = hostName
			option.dataset.hasSubscriptionUrl = 'false'
			hostSelect.appendChild(option)
		}
		hostSelect.value = hostName
	}
	_issueSetSelectedKeyHint(`Выбран ключ #${keyId} (${hostName}${keyEmail ? ` · ${keyEmail}` : ''}) для замены.`)
	_issueUpdateExecuteButtonLabel()
	_issueUpdateTierHint()
}

function issueByIdCloseKeysModal() {
	const modal = document.getElementById('issue-keys-modal')
	if (!modal) return
	modal.hidden = true
	document.body.classList.remove('modal-open')
	if (
		_issueModalLastActiveElement
		&& typeof _issueModalLastActiveElement.focus === 'function'
		&& document.contains(_issueModalLastActiveElement)
	) {
		_issueModalLastActiveElement.focus()
	}
	_issueModalLastActiveElement = null
}

function _issueOpenKeysModal() {
	const modal = document.getElementById('issue-keys-modal')
	if (!modal) return
	if (document.activeElement instanceof HTMLElement) {
		_issueModalLastActiveElement = document.activeElement
	}
	modal.hidden = false
	document.body.classList.add('modal-open')
	const closeBtn = document.getElementById('issue-keys-modal-close')
	if (closeBtn) {
		closeBtn.focus()
	}
}

function _issueRenderResult(data) {
	const empty = document.getElementById('issue-empty')
	const wrap = document.getElementById('issue-result')
	if (!empty || !wrap) return
	empty.hidden = true
	wrap.hidden = false

	document.getElementById('issue-result-telegram-id').textContent = data.telegram_id || '—'
	document.getElementById('issue-result-host').textContent = data.host_name || '—'
	document.getElementById('issue-result-email').textContent = data.key_email || '—'
	document.getElementById('issue-result-bot-source').textContent = data.bot_source === 'reserve' ? 'Резервный' : 'Основной'
	document.getElementById('issue-result-subscription-tier').textContent =
		data.subscription_tier === 'standard' ? 'Стандарт' : 'Супер'
	document.getElementById('issue-result-expiry').textContent = data.expiry_label || _migFormatDate(data.expiry)
	if (data.mode === 'extend') {
		document.getElementById('issue-result-mode').textContent = `Продление (#${data.key_id || '—'})`
	} else {
		document.getElementById('issue-result-mode').textContent = data.replace_existing
			? `Замена (${data.replaced_key_id || data.target_key_id || '—'} -> ${data.key_id || '—'})`
			: 'Новый ключ'
	}

	let notifyText = 'Не отправлялось'
	if (data.sent) {
		notifyText = 'Отправлено'
	} else if (data.send_error) {
		notifyText = `Ошибка: ${data.send_error}`
	}
	document.getElementById('issue-result-notify').textContent = notifyText

	const keyField = document.getElementById('issue-result-key')
	keyField.value = data.issued_key_value || ''
}

function _issueAppendModalCell(tr, label, value) {
	const td = document.createElement('td')
	td.dataset.label = label
	td.textContent = value
	tr.appendChild(td)
	return td
}

function _issueRenderKeysModalTable(keys, telegramId) {
	const tbody = document.getElementById('issue-keys-modal-body')
	if (!tbody) return
	tbody.innerHTML = ''
	if (!keys || keys.length === 0) {
		const tr = document.createElement('tr')
		const td = document.createElement('td')
		td.dataset.label = 'Статус'
		td.colSpan = 6
		td.textContent = 'Ключи не найдены'
		tr.appendChild(td)
		tbody.appendChild(tr)
		return
	}

	keys.forEach((item) => {
		const tr = document.createElement('tr')
		_issueAppendModalCell(tr, 'ID', String(item.key_id || '—'))
		_issueAppendModalCell(tr, 'Хост', item.host_name || '—')
		_issueAppendModalCell(tr, 'Email', item.key_email || '—')
		_issueAppendModalCell(tr, 'Срок', item.expiry_label || '—')
		_issueAppendModalCell(tr, 'Статус', item.is_active ? 'Активен' : (item.is_archived ? 'Архив' : 'Истёк'))

		const actionTd = document.createElement('td')
		actionTd.dataset.label = 'Действия'
		const actionWrap = document.createElement('div')
		actionWrap.className = 'issue-modal-actions'

		const selectBtn = document.createElement('button')
		selectBtn.type = 'button'
		selectBtn.className = 'button button-secondary issue-modal-action'
		selectBtn.textContent = 'Выбрать'
		selectBtn.addEventListener('click', () => {
			issueByIdSelectKeyForReplace(item.key_id, item.host_name, item.key_email || '')
			_issueSetStatus(`Ключ #${item.key_id} выбран для замены`)
			issueByIdCloseKeysModal()
		})
		actionWrap.appendChild(selectBtn)

		const extendBtn = document.createElement('button')
		extendBtn.type = 'button'
		extendBtn.className = 'button button-primary issue-modal-action'
		extendBtn.textContent = 'Продлить'
		extendBtn.addEventListener('click', () => {
			if (!confirm(`Продлить ключ #${item.key_id}?`)) return
			void issueByIdExtendKey(item.key_id, telegramId)
		})
		actionWrap.appendChild(extendBtn)

		const reissueBtn = document.createElement('button')
		reissueBtn.type = 'button'
		reissueBtn.className = 'button button-success issue-modal-action'
		reissueBtn.textContent = 'Перевыпустить'
		reissueBtn.addEventListener('click', () => {
			if (!confirm(`Перевыпустить ключ #${item.key_id}? Старый ключ будет архивирован.`)) return
			issueByIdSelectKeyForReplace(item.key_id, item.host_name, item.key_email || '')
			void issueByIdExecute()
		})
		actionWrap.appendChild(reissueBtn)

		if (item.key_email) {
			const copyBtn = document.createElement('button')
			copyBtn.type = 'button'
			copyBtn.className = 'button issue-modal-action'
			copyBtn.textContent = 'Скопировать email'
			copyBtn.addEventListener('click', async () => {
				try {
					await navigator.clipboard.writeText(item.key_email)
					_issueSetModalStatus('Email скопирован')
				} catch (error) {
					_issueSetModalStatus(`Не удалось скопировать email: ${error.message}`, true)
				}
			})
			actionWrap.appendChild(copyBtn)
		}

		actionTd.appendChild(actionWrap)
		tr.appendChild(actionTd)
		tbody.appendChild(tr)
	})
}

async function _issueReadJsonResponse(response, fallbackMessage) {
	const rawText = await response.text()
	let data = {}
	if (rawText) {
		try {
			data = JSON.parse(rawText)
		} catch (error) {
			const shortBody = rawText.trim().slice(0, 180)
			const suffix = shortBody ? `: ${shortBody}` : ''
			throw new Error(`${fallbackMessage}: сервер вернул не JSON (${response.status})${suffix}`)
		}
	}
	if (!response.ok) {
		throw new Error(data.error || `${fallbackMessage} (${response.status})`)
	}
	return data
}

function _issueDelay(milliseconds) {
	return new Promise(resolve => window.setTimeout(resolve, milliseconds))
}

async function _issuePollRequestResult(requestId, attempts = 90) {
	for (let attempt = 0; attempt < attempts; attempt += 1) {
		const response = await fetch(`/api/issue-by-id/status/${encodeURIComponent(requestId)}`, {
			method: 'GET',
			headers: {'Accept': 'application/json'},
			cache: 'no-store',
		})
		if (response.status === 202) {
			_issueSetStatus('Выдача продолжается на сервере. Не повторяйте операцию...')
			await _issueDelay(2000)
			continue
		}
		return _issueReadJsonResponse(response, 'Ошибка проверки выдачи')
	}
	throw new Error('сервер всё ещё выполняет выдачу; идентификатор операции сохранён, повторите проверку позже')
}

async function issueByIdOpenKeysModal() {
	let payload
	try {
		payload = _issueGetFormBasePayload()
	} catch (error) {
		_issueSetStatus(error.message, true)
		return
	}
	_issueOpenKeysModal()
	_issueSetModalStatus('Загрузка ключей пользователя...')

	try {
		const response = await fetch('/api/issue-by-id/context', {
			method: 'POST',
			headers: {'Content-Type': 'application/json'},
			body: JSON.stringify({telegram_id: payload.telegram_id}),
		})
		const data = await _issueReadJsonResponse(response, 'Ошибка загрузки ключей')
		const userLine = `ID: ${data.telegram_id} · username: ${data.username || '—'} · активных: ${data.keys_active}/${data.keys_total}`
		const userNode = document.getElementById('issue-keys-modal-user')
		if (userNode) userNode.textContent = userLine
		_issueRenderKeysModalTable(data.keys || [], data.telegram_id)
		_issueSetModalStatus('Выберите действие по ключу')
	} catch (error) {
		_issueSetModalStatus(`Ошибка: ${error.message}`, true)
	}
}

async function issueByIdExecute() {
	const mode = document.getElementById('issue-mode')?.value || 'new'
	const btn = document.getElementById('issue-btn-execute')
	if (_issueActionInFlight) {
		_issueSetStatus('Дождитесь завершения текущей операции')
		return
	}
	let payload
	let duration
	let requestId = null
	try {
		payload = _issueGetFormBasePayload()
		duration = _issueReadDurationInputs()
	} catch (error) {
		_issueSetStatus(error.message, true)
		return
	}
	if (mode === 'new' && !payload.host_name) {
		_issueSetStatus('Выберите хост', true)
		return
	}
	if (mode === 'replace' && !payload.host_name && !payload.target_key_id) {
		_issueSetStatus('Выберите хост или ключ для замены', true)
		return
	}
	if (mode === 'replace' && !confirm('Подтвердите перевыпуск: старый ключ будет архивирован после успешной выдачи.')) {
		return
	}

	_issueSetActionLock(true)
	btn.textContent = mode === 'replace' ? 'Перевыпуск...' : 'Выдача...'
	_issueSetStatus(mode === 'replace' ? 'Перевыпускаю ключ...' : 'Создаю ключ на панели...')

	try {
		const requestPayload = {
			...payload,
			...duration,
			replace_existing: mode === 'replace',
		}
		requestId = _issueRequestIdForPayload(requestPayload)
		const response = await fetch('/api/issue-by-id/execute', {
			method: 'POST',
			headers: {'Content-Type': 'application/json'},
			body: JSON.stringify({
				...requestPayload,
				request_id: requestId,
			}),
		})
		const data = response.headers.get('X-Issue-Request-In-Progress') === 'true'
			? await _issuePollRequestResult(requestId)
			: await _issueReadJsonResponse(response, 'Ошибка выдачи')
		_issueCompleteRequest(requestId)
		_issueRenderResult(data)
		const suffix = data.user_created ? ' (создан новый пользователь в users)' : ''
		const successMessage = mode === 'replace' ? `Ключ успешно перевыпущен${suffix}` : `Ключ успешно выдан${suffix}`
		_issueSetStatus(successMessage)
	} catch (error) {
		if (requestId) {
			try {
				const data = await _issuePollRequestResult(requestId)
				_issueCompleteRequest(requestId)
				_issueRenderResult(data)
				_issueSetStatus(mode === 'replace' ? 'Ключ успешно перевыпущен' : 'Ключ успешно выдан')
				return
			} catch (statusError) {
				_issueSetStatus(`Результат не подтверждён: ${statusError.message}. Не нажимайте повторно — операция сохранена.`, true)
				return
			}
		}
		_issueSetStatus(`Ошибка: ${error.message}`, true)
	} finally {
		_issueSetActionLock(false)
		_issueUpdateExecuteButtonLabel()
	}
}

async function issueByIdExtendKey(targetKeyId, forcedTelegramId = null) {
	const btn = document.getElementById('issue-btn-execute')
	if (_issueActionInFlight) {
		_issueSetStatus('Дождитесь завершения текущей операции')
		return
	}
	let payload
	let duration
	try {
		payload = _issueGetFormBasePayload()
		duration = _issueReadDurationInputs()
	} catch (error) {
		_issueSetStatus(error.message, true)
		return
	}

	const telegramId = forcedTelegramId || payload.telegram_id
	const requestPayload = {
		...duration,
		telegram_id: telegramId,
		target_key_id: targetKeyId,
		subscription_tier: payload.subscription_tier,
		bot_source: payload.bot_source,
		send_message: payload.send_message,
		message_text: payload.message_text,
	}

	_issueSetActionLock(true)
	btn.textContent = 'Продление...'
	_issueSetStatus(`Продлеваю ключ #${targetKeyId}...`)

	try {
		const response = await fetch('/api/issue-by-id/extend', {
			method: 'POST',
			headers: {'Content-Type': 'application/json'},
			body: JSON.stringify(requestPayload),
		})
		const data = await _issueReadJsonResponse(response, 'Ошибка продления')
		_issueRenderResult(data)
		_issueSetStatus(`Ключ #${targetKeyId} продлён`)
		issueByIdCloseKeysModal()
	} catch (error) {
		_issueSetStatus(`Ошибка: ${error.message}`, true)
		_issueSetModalStatus(`Ошибка продления: ${error.message}`, true)
	} finally {
		_issueSetActionLock(false)
		_issueUpdateExecuteButtonLabel()
	}
}

async function issueByIdCopyKey() {
	const field = document.getElementById('issue-result-key')
	const btn = document.getElementById('issue-btn-copy-key')
	if (!field || !btn) return
	const value = (field.value || '').trim()
	if (!value) {
		_issueSetStatus('Ключ пустой, нечего копировать', true)
		return
	}
	try {
		await navigator.clipboard.writeText(value)
		const original = btn.textContent
		btn.textContent = 'Скопировано'
		btn.disabled = true
		setTimeout(() => {
			btn.textContent = original
			btn.disabled = false
		}, 1200)
	} catch (error) {
		_issueSetStatus(`Не удалось скопировать: ${error.message}`, true)
	}
}
