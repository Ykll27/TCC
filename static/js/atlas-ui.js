(function(){
  const sidebar = document.querySelector('.atlas-sidebar');
  document.querySelectorAll('[data-sidebar-toggle]').forEach(btn=>btn.addEventListener('click',()=>sidebar&&sidebar.classList.toggle('open')));
  document.addEventListener('click', (ev)=>{
    if(!sidebar || !sidebar.classList.contains('open')) return;
    if(ev.target.closest('.atlas-sidebar') || ev.target.closest('[data-sidebar-toggle]')) return;
    sidebar.classList.remove('open');
  });

  const globalSearch = document.querySelector('[data-global-search]');
  if(globalSearch){
    globalSearch.addEventListener('input', ()=>{
      const q = globalSearch.value.trim().toLowerCase();
      document.querySelectorAll('[data-search-item]').forEach(item=>{
        item.style.display = !q || item.textContent.toLowerCase().includes(q) ? '' : 'none';
      });
    });
  }

  document.querySelectorAll('[data-table-filter]').forEach(input=>{
    const selector = input.getAttribute('data-table-filter');
    input.addEventListener('input',()=>{
      const q=input.value.trim().toLowerCase();
      document.querySelectorAll(selector+' tbody tr').forEach(tr=>{
        tr.style.display=!q || tr.textContent.toLowerCase().includes(q)?'':'none';
      });
    });
  });

  document.querySelectorAll('form[data-confirm]').forEach(form=>{
    form.addEventListener('submit', ev=>{
      const msg=form.getAttribute('data-confirm')||'Confirmar ação?';
      if(!confirm(msg)) ev.preventDefault();
    });
  });
})();
