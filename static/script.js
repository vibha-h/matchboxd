(function(){
  const inputsContainer = document.getElementById('inputs')
  const addBtn = document.getElementById('add')
  const submitBtn = document.getElementById('submit')
  const status = document.getElementById('status')
  const results = document.getElementById('results')
  const sharedList = document.getElementById('shared-list')
  const countsDiv = document.getElementById('counts')

  let inputCount = inputsContainer.querySelectorAll('.input-row').length

  function makeInput(index){
    const row = document.createElement('div')
    row.className = 'input-row'
    const label = document.createElement('label')
    label.textContent = `username ${index}`
    const input = document.createElement('input')
    input.type = 'text'
    input.className = 'username-input'
    input.setAttribute('data-index', index)
    input.autocomplete = 'off'
    const err = document.createElement('div')
    err.className = 'error'
    err.setAttribute('aria-live','polite')
    input.addEventListener('input', onInputChange)
    row.appendChild(label)
    row.appendChild(input)
    row.appendChild(err)
    return row
  }

  function onInputChange(e){
    const input = e.target
    const val = input.value.trim()
    const err = input.parentElement.querySelector('.error')
    input.classList.remove('invalid')
    err.textContent = ''
    checkEnableSubmit()
    if(val.length === 0) return
    if(val.length < 2){
      err.textContent = 'enter at least 2 characters'
      input.classList.add('invalid')
      checkEnableSubmit()
      return
    }
    // debounce validation
    if(input._timer) clearTimeout(input._timer)
    input._pendingValidation = val
    input._timer = setTimeout(()=> validateUsername(input, val), 450)
  }

  function validateUsername(input, username){
    const err = input.parentElement.querySelector('.error')
    err.textContent = 'checking...'
    const requested = username
    fetch('/validate_username', {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({username: requested})})
      .then(r=>r.json()).then(data=>{
        if (input.value.trim() !== requested) {
          // ignore outdated validation results
          return
        }
        input._pendingValidation = null
        if(data.valid){
          err.textContent = ''
          input.classList.remove('invalid')
        } else {
          err.textContent = 'username not valid'
          input.classList.add('invalid')
        }
        checkEnableSubmit()
      }).catch(()=>{
        if (input.value.trim() !== requested) {
          return
        }
        err.textContent = 'error checking username'
        input.classList.add('invalid')
        checkEnableSubmit()
      })
  }

  function checkEnableSubmit(){
    const inputs = Array.from(document.querySelectorAll('.username-input'))
    // count filled and valid
    let filled = 0
    let anyInvalid = false
    inputs.forEach(i=>{
      if(i.value.trim().length >= 2) filled++
      if(i.classList.contains('invalid')) anyInvalid = true
    })
    submitBtn.disabled = !(filled >= 2 && !anyInvalid)
  }

  addBtn.addEventListener('click', ()=>{
    inputCount++
    const node = makeInput(inputCount)
    inputsContainer.appendChild(node)
    node.querySelector('input').focus()
  })

  submitBtn.addEventListener('click', ()=>{
    status.textContent = 'computing shared watchlist...'
    const inputs = Array.from(document.querySelectorAll('.username-input'))
    const usernames = inputs.map(i=>i.value.trim()).filter(s=>s.length>=2)
    submitBtn.disabled = true
    fetch('/shared_watchlist',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({usernames})})
      .then(async r=>{
        submitBtn.disabled = false
        if(!r.ok){
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
        status.textContent = ''
        results.hidden = false
        sharedList.innerHTML = ''
        countsDiv.textContent = Object.entries(data.counts).map(([u,c])=>`${u}: ${c}`).join(' • ')
        if(data.shared.length===0){
          sharedList.innerHTML = '<li>No shared films found</li>'
        } else {
          data.shared.forEach(f=>{
            const li = document.createElement('li')
            // new format: object with {title, url}
            if(f && typeof f === 'object' && (f.title || f.url)){
              const a = document.createElement('a')
              a.textContent = f.title || f.url || 'link'
              if(f.url){
                a.href = f.url
                a.target = '_blank'
                a.rel = 'noopener noreferrer'
              } else {
                a.href = '#'
              }
              li.appendChild(a)
            } else {
              // fallback for older servers returning plain strings
              li.textContent = f
            }
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
        if(results.hidden){
          status.textContent = 'unexpected error'
        }
        submitBtn.disabled = false
      })
  })

  // attach existing inputs
  document.querySelectorAll('.username-input').forEach(i=>i.addEventListener('input', onInputChange))

})();
