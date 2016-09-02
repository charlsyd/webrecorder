import { LOAD_USERS, LOAD_USERS_SUCCESS, LOAD_USERS_ERROR } from './constants';


export function loadUsers(qs) {
  return {
    type: LOAD_USERS,
    params: qs,
  };
}

export function usersLoaded(users) {
  return {
    type: LOAD_USERS_SUCCESS,
    users,
  };
}

export function userLoadingError(error) {
  return {
    type: LOAD_USERS_ERROR,
    error,
  };
}
