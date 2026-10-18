(function(){
  const inputsContainer = document.getElementById('inputs')
  const addBtn = document.getElementById('add')
  const submitBtn = document.getElementById('submit')
  const status = document.getElementById('status')
  const results = document.getElementById('results')
  const sharedList = document.getElementById('shared-list')
  const countsDiv = document.getElementById('counts')
  const resultsTitle = document.querySelector('#results h2')

  const MAX_INPUTS = 8
  let inputCount = inputsContainer.querySelectorAll('.input-row').length

  function setLoadingState(isLoading){
    if (!isLoading) {
      status.textContent = ''
      status.classList.remove('loading')
      return
    }

    status.textContent = 'computing shared watchlist'
    status.classList.add('loading')
  }

  function makeInput(index){
    const row = document.createElement('div')
    row.className = 'input-row'

    const rowHeader = document.createElement('div')
    rowHeader.className = 'input-row-header'

    const label = document.createElement('label')
    label.textContent = `username ${index}`

    const removeBtn = document.createElement('button')
    removeBtn.type = 'button'
    removeBtn.className = 'remove-input-btn'
    removeBtn.setAttribute('aria-label', `Remove username ${index}`)
    removeBtn.textContent = '×'
    removeBtn.hidden = index <= 2
    removeBtn.addEventListener('click', ()=> removeInputRow(row))

    rowHeader.appendChild(label)
    rowHeader.appendChild(removeBtn)

    const inputWrap = document.createElement('div')
    inputWrap.className = 'input-with-remove'

    const input = document.createElement('input')
    input.type = 'text'
    input.className = 'username-input'
    input.setAttribute('data-index', index)
    input.autocomplete = 'off'

    const err = document.createElement('div')
    err.className = 'error'
    err.setAttribute('aria-live','polite')
    input.addEventListener('input', onInputChange)

    inputWrap.appendChild(input)
    row.appendChild(rowHeader)
    row.appendChild(inputWrap)
    row.appendChild(err)
    return row
  }

  function removeInputRow(row){
    const rows = Array.from(document.querySelectorAll('.input-row'))
    if (rows.length <= 2) return
    row.remove()
    refreshInputLabels()
    checkEnableSubmit()
  }

  function refreshInputLabels(){
    const rows = Array.from(document.querySelectorAll('.input-row'))
    rows.forEach((row, index)=>{
      const label = row.querySelector('label')
      const removeBtn = row.querySelector('.remove-input-btn')
      const input = row.querySelector('.username-input')
      if (label) label.textContent = `username ${index + 1}`
      if (input) input.setAttribute('data-index', index + 1)
      if (removeBtn) removeBtn.hidden = index < 2
      if (removeBtn) removeBtn.setAttribute('aria-label', `Remove username ${index + 1}`)
    })
    inputCount = rows.length
    addBtn.disabled = rows.length >= MAX_INPUTS
  }

  function getInputErrorElement(input){
    const row = input.closest('.input-row')
    return row ? row.querySelector('.error') : null
  }

  function onInputChange(e){
    const input = e.target
    const val = input.value.trim()
    const err = getInputErrorElement(input)
    input.classList.remove('invalid')
    input.dataset.validating = 'false'
    if (err) err.textContent = ''
    checkEnableSubmit()
    if(val.length === 0) return
    if(val.length < 2){
      if (err) err.textContent = 'enter at least 2 characters'
      input.classList.add('invalid')
      checkEnableSubmit()
      return
    }
    // debounce validation
    if(input._timer) clearTimeout(input._timer)
    input._pendingValidation = val
    input.dataset.validating = 'true'
    input._timer = setTimeout(()=> validateUsername(input, val), 450)
    checkEnableSubmit()
  }

  function validateUsername(input, username){
    const err = getInputErrorElement(input)
    if (err) err.textContent = 'checking...'
    const requested = username
    input.dataset.validating = 'true'
    fetch('/validate_username', {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({username: requested})})
      .then(r=>r.json()).then(data=>{
        if (input.value.trim() !== requested) {
          // ignore outdated validation results
          return
        }
        input._pendingValidation = null
        input.dataset.validating = 'false'
        if(data.valid){
          if (err) err.textContent = ''
          input.classList.remove('invalid')
        } else {
          if (err) err.textContent = 'username not valid'
          input.classList.add('invalid')
        }
        checkEnableSubmit()
      }).catch(()=>{
        if (input.value.trim() !== requested) {
          return
        }
        input._pendingValidation = null
        input.dataset.validating = 'false'
        if (err) err.textContent = 'error checking username'
        input.classList.add('invalid')
        checkEnableSubmit()
      })
  }

  function checkEnableSubmit(){
    const inputs = Array.from(document.querySelectorAll('.username-input'))
    const anyShort = inputs.some(i => i.value.trim().length > 0 && i.value.trim().length < 2)
    const hasPendingValidation = inputs.some(i => i.value.trim().length >= 2 && i.dataset.validating === 'true')
    const anyInvalid = inputs.some(i => i.value.trim().length >= 2 && i.classList.contains('invalid'))
    const validInputs = inputs.filter(i => i.value.trim().length >= 2 && !i.classList.contains('invalid') && i.dataset.validating !== 'true')

    submitBtn.disabled = !(validInputs.length >= 2 && !anyShort && !hasPendingValidation && !anyInvalid)
  }

  addBtn.addEventListener('click', ()=>{
    const rows = Array.from(document.querySelectorAll('.input-row'))
    if (rows.length >= MAX_INPUTS) return
    inputCount++
    const node = makeInput(inputCount)
    inputsContainer.appendChild(node)
    node.querySelector('input').focus()
    refreshInputLabels()
  })

  submitBtn.addEventListener('click', ()=>{
    // hide stale results immediately and show loading state for every request
    results.hidden = true
    sharedList.innerHTML = ''
    countsDiv.textContent = ''
    const oldLogs = document.getElementById('logs')
    if(oldLogs) oldLogs.remove()
    setLoadingState(true)

    const inputs = Array.from(document.querySelectorAll('.username-input'))
    const usernames = inputs.map(i=>i.value.trim()).filter(s=>s.length>=2)
    submitBtn.disabled = true
    fetch('/shared_watchlist',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({usernames})})
      .then(async r=>{
        submitBtn.disabled = false
        if(!r.ok){
          setLoadingState(false)
          const e = await r.json().catch(()=>({error:'server error'}))
          if(e.invalid){
            // mark invalid fields
            e.invalid.forEach(inv=>{
              const el = Array.from(document.querySelectorAll('.username-input')).find(i=>i.value.trim().toLowerCase()===inv.toLowerCase())
              if(el){
                el.classList.add('invalid')
                el.parentElement.querySelector('.error').textContent = 'username not valid'
              }
            })
            status.textContent = 'one or more usernames invalid'
          } else {
            status.textContent = e.error || 'server error'
          }
          return
        }
        return r.json()
      }).then(data=>{
        if(!data) return
        setLoadingState(false)
        results.hidden = false
        sharedList.innerHTML = ''

        const visibleUsernames = Array.isArray(data.usernames) ? data.usernames : []
        const sharedCount = Array.isArray(data.shared) ? data.shared.length : 0

        if (resultsTitle) {
          resultsTitle.textContent = `Shared Films (${sharedCount})`
        }
        countsDiv.textContent = `${visibleUsernames.join(', ')}`

        if(data.shared.length===0){
          sharedList.innerHTML = '<li>No shared films found</li>'
        } else {
          data.shared.forEach(f=>{
            const li = document.createElement('li')
            li.className = 'film-tile'

            const link = document.createElement('a')
            link.className = 'film-poster-link'
            link.href = f && f.url ? f.url : '#'
            link.target = f && f.url ? '_blank' : '_self'
            link.rel = 'noopener noreferrer'

            const img = document.createElement('img')
            img.className = 'film-poster'
            img.alt = f && f.title ? f.title : 'Shared film poster'
            img.src = (f && f.poster) || 'https://via.placeholder.com/300x450?text=Poster'
            img.loading = 'lazy'
            img.onerror = ()=> { img.src = 'https://via.placeholder.com/300x450?text=Poster' }

            const label = document.createElement('div')
            label.className = 'film-title'
            label.textContent = f && f.title ? f.title : 'Unknown title'

            link.appendChild(img)
            li.appendChild(link)
            li.appendChild(label)
            sharedList.appendChild(li)
          })
        }

        // render logs returned from the server (per-user pages & counts)
        let logsDiv = document.getElementById('logs')
        if(!logsDiv){
          logsDiv = document.createElement('div')
          logsDiv.id = 'logs'
          logsDiv.className = 'logs'
          results.appendChild(logsDiv)
        }
        logsDiv.innerHTML = ''
        if(Array.isArray(data.logs)){
          data.logs.forEach(l=>{
            const userSection = document.createElement('div')
            userSection.className = 'log-user'
            const h = document.createElement('h3')
            // h.textContent = `${l.username} ${l.cached ? '(cached)' : ''} — total: ${l.total}`
            userSection.appendChild(h)
            const ul = document.createElement('ul')
            ul.className = 'log-pages';
            // (l.pages||[]).forEach(p=>{
            //   const li = document.createElement('li')
            //   li.textContent = `page ${p.page}: status ${p.status}, found ${p.found}` + (p.error ? ` — error: ${p.error}` : '')
            //   ul.appendChild(li)
            // })
            userSection.appendChild(ul)
            logsDiv.appendChild(userSection)
          })
        }
        // end of logs rendering
      }).catch((err)=>{
        console.error('Shared watchlist request or render failed', err)
        setLoadingState(false)
        if(results.hidden){
          status.textContent = 'unexpected error'
        }
        submitBtn.disabled = false
      })
  })

  // attach existing inputs
  document.querySelectorAll('.username-input').forEach(i=>i.addEventListener('input', onInputChange))
  refreshInputLabels()

})();
