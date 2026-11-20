import {nodesAPI,scenariosAPI,drillsAPI,quotaAPI} from '../api/entities.js';
export const store={nodes:[],scenarios:[],drills:[],quota:null,async refresh(){[this.nodes,this.scenarios,this.drills,this.quota]=await Promise.all([nodesAPI.list(),scenariosAPI.list(),drillsAPI.list(),quotaAPI.overview()]);}};
