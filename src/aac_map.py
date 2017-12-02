#
# aac_map.py, doom-net
#
# Created by Andrey Kolishchak on 01/21/17.
#
import torch.nn as nn
import torch.nn.functional as F
from cuda import *
from collections import namedtuple
from egreedy import EGreedy
from aac_base import AACBase


class BaseModel(AACBase):
    def __init__(self, in_channels, button_num, variable_num, frame_num):
        super(BaseModel, self).__init__()
        self.screen_feature_num = 128
        self.conv1 = nn.Conv2d(in_channels=in_channels, out_channels=64, kernel_size=(1, 1), stride=(1, 1))
        self.conv2 = nn.Conv2d(in_channels=64, out_channels=32, kernel_size=(1, 1), stride=(1, 1))
        self.conv3 = nn.Conv2d(in_channels=32, out_channels=1, kernel_size=(1, 1), stride=(1, 1))

        self.screen_features1 = nn.Linear(12 * 16, self.screen_feature_num)

        self.batch_norm = nn.BatchNorm1d(self.screen_feature_num)

        layer1_size = 64
        self.action1 = nn.Linear(self.screen_feature_num, layer1_size)
        self.action2 = nn.Linear(layer1_size + variable_num, button_num)

        self.value1 = nn.Linear(self.screen_feature_num, layer1_size)
        self.value2 = nn.Linear(layer1_size + variable_num, 1)

        self.screens = None
        self.frame_num = frame_num


    def forward(self, screen, variables):
        # cnn
        screen_features = F.max_pool2d(screen, kernel_size=(20, 20), stride=(20, 20))
        screen_features = F.selu(self.conv1(screen_features))
        screen_features = F.selu(self.conv2(screen_features))
        screen_features = F.selu(self.conv3(screen_features))
        screen_features = screen_features.view(screen_features.size(0), -1)

        # features
        input = self.screen_features1(screen_features)
        input = self.batch_norm(input)
        input = F.selu(input)

        # action
        action = F.selu(self.action1(input))
        action = torch.cat([action, variables], 1)
        action = self.action2(action)

        return action, input

    def transform_input(self, screen, variables):
        screen_batch = []
        if self.frame_num > 1:
            if self.screens is None:
                self.screens = [[]] * len(screen)
            for idx, screens in enumerate(self.screens):
                if len(screens) >= self.frame_num:
                    screens.pop(0)
                screens.append(screen[idx])
                if len(screens) == 1:
                    for i in range(self.frame_num - 1):
                        screens.append(screen[idx])
                screen_batch.append(torch.cat(screens, 0))
            screen = torch.stack(screen_batch)

        screen = Variable(screen, volatile=not self.training)
        variables = Variable(variables / 100, volatile=not self.training)
        return screen, variables

    def set_terminal(self, terminal):
        if self.screens is not None:
            indexes = torch.nonzero(terminal == 0).squeeze()
            for idx in range(len(indexes)):
                self.screens[indexes[idx]] = []


ModelOutput = namedtuple('ModelOutput', ['action', 'value'])


class AdvantageActorCriticMap(BaseModel):
    def __init__(self, args):
        super(AdvantageActorCriticMap, self).__init__(args.screen_size[0]*args.frame_num, args.button_num, args.variable_num, args.frame_num)
        if args.base_model is not None:
            # load weights from the base model
            base_model = torch.load(args.base_model)
            self.load_state_dict(base_model.state_dict())
            del base_model

        self.discount = args.episode_discount
        self.outputs = []
        self.rewards = []
        self.discounts = []

    def reset(self):
        self.outputs = []
        self.rewards = []
        self.discounts = []

    def forward(self, screen, variables):
        action, input = super(AdvantageActorCriticMap, self).forward(screen, variables)

        action = F.softmax(action)
        if self.training:
            #action = action.multinomial()
            action = EGreedy(0.1)(action)
        else:
            _, action = action.max(1, keepdim=True)
            return action, None

        # value prediction - critic
        value = F.selu(self.value1(input))
        value = torch.cat([value, variables], 1)
        value = self.value2(value)

        # save output for backpro
        self.outputs.append(ModelOutput(action, value))
        return action, value

    def get_action(self, state):
        action, _ = self.forward(*self.transform_input(state.screen, state.variables))
        return action.data

    def set_reward(self, reward):
        self.rewards.append(reward * 0.01)  # no clone() b/c of * 0.01

    def set_terminal(self, terminal):
        super(AdvantageActorCriticMap, self).set_terminal(terminal)
        self.discounts.append(self.discount * terminal)

    def backward(self):
        #
        # calculate step returns in reverse order
        #rewards = torch.stack(self.rewards, dim=0)
        rewards = self.rewards

        returns = torch.Tensor(len(rewards) - 1, *self.outputs[-1].value.data.size())
        step_return = self.outputs[-1].value.data.cpu()
        for i in range(len(rewards) - 2, -1, -1):
            step_return.mul_(self.discounts[i]).add_(rewards[i])
            returns[i] = step_return

        if USE_CUDA:
            returns = returns.cuda()
        #
        # calculate losses
        value_loss = 0
        for i in range(len(self.outputs) - 1):
            self.outputs[i].action.reinforce(returns[i] - self.outputs[i].value.data)
            value_loss += F.smooth_l1_loss(self.outputs[i].value, Variable(returns[i]))
        #
        # backpro all variables at once
        variables = [value_loss] + [output.action for output in self.outputs[:-1]]
        gradients = [torch.ones(1).cuda() if USE_CUDA else torch.ones(1)] + [None for _ in self.outputs[:-1]]
        autograd.backward(variables, gradients)
        # reset state
        self.reset()