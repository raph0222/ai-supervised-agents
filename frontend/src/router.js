import { ref } from 'vue'

// Two routes and no nesting. vue-router would be a dependency, a build-size
// increase and a config file to answer a question this answers in ten lines.
export const path = ref(window.location.pathname)

export function navigate(to) {
  if (to === path.value) return
  window.history.pushState({}, '', to)
  path.value = to
}

window.addEventListener('popstate', () => {
  path.value = window.location.pathname
})
