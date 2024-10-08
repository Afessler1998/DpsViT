# © 2024 Alec Fessler
# MIT License
# See LICENSE file in the project root for full license information.

import torch
import torch.nn as nn
import torchvision
from torch.utils.data import DataLoader
from torch.amp.grad_scaler import GradScaler
from torch.amp.autocast_mode import autocast
from torch.optim.lr_scheduler import CosineAnnealingLR, LambdaLR
from tqdm import tqdm
import yaml
from modules.APViT import APViT
from modules.AdaptivePatchLoss import AdaptivePatchLoss
from timm.data import Mixup, create_transform
import optuna

class APViTCifar10(nn.Module):
    def __init__(
        self,
        num_patches,
        hidden_channels,
        attn_embed_dim,
        num_transformer_layers,
        stochastic_depth,
        pos_embed_size,
        scaling,
        max_scale,
        rotating
    ):
        super(APViTCifar10, self).__init__()
        self.vit = APViT(
            num_patches=num_patches,
            hidden_channels=hidden_channels,
            attn_embed_dim=attn_embed_dim,
            pos_embed_dim=attn_embed_dim,
            num_transformer_layers=num_transformer_layers,
            stochastic_depth=stochastic_depth,
            pos_embed_size=pos_embed_size,
            scaling=scaling,
            max_scale=max_scale,
            rotating=rotating
        )

    def forward(self, x):
        return self.vit(x)

def load_config(config_file):
    with open(config_file, "r") as file:
        config = yaml.safe_load(file)
    return config

def get_dataloaders(
        batch_size,
        num_workers=2,
        augment_magnitude=9,
        re_prob=0.25
    ):
    train_transform = create_transform(
        input_size=32,
        is_training=True,
        auto_augment=f'rand-m{augment_magnitude}-mstd0.5-inc1',
        re_prob=re_prob,
        re_mode='pixel',
        re_count=1,
        mean=[0.4914, 0.4822, 0.4465],
        std=[0.2470, 0.2435, 0.2616]
    )

    test_transform = create_transform(
        input_size=32,
        is_training=False,
        mean=[0.4914, 0.4822, 0.4465],
        std=[0.2470, 0.2435, 0.2616]
    )

    trainset = torchvision.datasets.CIFAR10(
        root='./data',
        train=True, download=True, transform=train_transform
    )
    trainloader = DataLoader(
        trainset,
        batch_size=batch_size,
        shuffle=True,
        num_workers=num_workers
    )

    testset = torchvision.datasets.CIFAR10(
        root='./data',
        train=False,
        download=True,
        transform=test_transform
    )
    testloader = DataLoader(
        testset,
        batch_size=batch_size,
        shuffle=False,
        num_workers=num_workers
    )

    return trainloader, testloader

def evaluate(
        model,
        test_loader,
        criterion,
        device,
    ):
    model.eval()
    running_vit_loss = 0.0
    running_ap_loss = 0.0
    correct = 0
    total = 0

    ap_criterion, vit_criterion = criterion

    with torch.no_grad():
        for inputs, labels in test_loader:
            inputs, labels = inputs.to(device), labels.to(device)
            outputs, attn_weights = model(inputs)
            vit_loss = vit_criterion(outputs, labels)
            ap_loss = ap_criterion(attn_weights)
            running_vit_loss += vit_loss.item()
            running_ap_loss += ap_loss
            _, predicted = outputs.max(1)
            total += labels.size(0)
            correct += predicted.eq(labels).sum().item()

    accuracy = correct / total
    vit_test_loss = running_vit_loss / len(test_loader)
    ap_test_loss = running_ap_loss / len(test_loader)

    return vit_test_loss, ap_test_loss, accuracy

def train(
        model,
        train_loader,
        criterion,
        optimizer,
        scheduler,
        warmup_scheduler,
        warmup_epochs,
        epoch,
        accumulation_steps,
        scaler,
        mixup_fn,
        ap_loss_weight,
        ema_decay,
        ema,
        ap_loss,
        device
    ):

    ap_criterion, vit_criterion = criterion
    model.train()
    running_vit_loss = 0.0
    running_ap_loss = 0.0

    with tqdm(train_loader, unit="batch") as tepoch:
        for i, (images, labels) in enumerate(tepoch):
            images, labels = images.to(device), labels.to(device)
            images, labels = mixup_fn(images, labels)

            if i % accumulation_steps == 0:
                optimizer.zero_grad()

            with autocast(device_type=device.type):
                outputs, attn_weights = model(images)
                vit_loss = vit_criterion(outputs, labels)
                ap_crit = ap_criterion(attn_weights)

            scaled_vit_loss = vit_loss / accumulation_steps
            scaler.scale(scaled_vit_loss).backward(retain_graph=True if epoch > warmup_epochs and ap_loss else False)

            if epoch > warmup_epochs and ap_loss:
                model.vit.requires_grad = False
                model.vit.adaptive_patches.requires_grad = True
                scaled_ap_loss = ap_crit / accumulation_steps * ap_loss_weight
                scaler.scale(scaled_ap_loss).backward()
                model.vit.requires_grad = True

            if (i + 1) % accumulation_steps == 0 or (i + 1) == len(train_loader):
                if ema:
                    ap_weights = {k: v.clone().detach() for k, v in model.vit.adaptive_patches.state_dict().items()}

                scaler.step(optimizer)
                scaler.update()

                if ema:
                    for name, param in model.vit.adaptive_patches.named_parameters():
                        param.data = ema_decay * param.data + (1 - ema_decay) * ap_weights[name]

            tepoch.set_postfix(loss=running_vit_loss / (i + 1))
            running_vit_loss += vit_loss.item()
            running_ap_loss += ap_crit

    if epoch < warmup_epochs:
        warmup_scheduler.step()
    else:
        scheduler.step()

    return running_vit_loss / len(train_loader), running_ap_loss / len(train_loader)

def objective(trial):
    torch.manual_seed(42)
    torch.cuda.manual_seed_all(42)
    torch.backends.cudnn.deterministic = True
    torch.backends.cudnn.benchmark = False

    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    config = load_config("hparams_config.yaml")

    batch_size = config.get("batch_size", 256)
    accumulation_steps = config.get("accumulation_steps", 2)
    epochs = config.get("epochs", 100)
    warmup_epochs = config.get("warmup_epochs", 5)
    weight_decay = config.get("weight_decay", 0.00015)
    lr_factor = config.get("lr_factor", 512)
    lr = 0.0005 * accumulation_steps * batch_size / lr_factor
    eta_min = config.get("eta_min", 0.00015)
    hidden_channels = config.get("hidden_channels", 16)
    attn_embed_dim = config.get("attn_embed_dim", 256)
    num_transformer_layers = config.get("num_transformer_layers", 6)
    stochastic_depth = config.get("stochastic_depth", 0.15)
    re_prob = config.get("re_prob", 0.15)
    augment_magnitude = config.get("augment_magnitude", 5)
    mixup_alpha = config.get("mixup_alpha", 0.5)
    cutmix_alpha = config.get("cutmix_alpha", 0.2)
    mixup_prob = config.get("mixup_prob", 0.5)
    mixup_switch_prob = config.get("mixup_switch_prob", 0.5)
    label_smoothing = config.get("label_smoothing", 0.05)
    lower_quantile = config.get("lower_quantile", 0.1)
    ap_loss_weight = config.get("ap_loss_weight", 0.01)
    ap_lr = config.get("ap_lr", 0.00015)
    ema_decay = config.get("ema_decay", 0.99)
    ap_weight_decay = config.get("ap_weight_decay", 0.007)

    scaling = trial.suggest_categorical("scaling", [None, 'isotropic', 'anisotropic'])
    rotating = trial.suggest_categorical("rotating", [True, False])
    ema = trial.suggest_categorical("ema", [True, False])
    ap_loss = trial.suggest_categorical("ap_loss", [True, False])

    print(f'Trial started: {trial.number}\nscaling: {scaling}\nrotating: {rotating}\nema: {ema}\nap_loss: {ap_loss}')

    trainloader, testloader = get_dataloaders(
        batch_size,
        num_workers=4,
        augment_magnitude=augment_magnitude,
        re_prob=re_prob
    )

    model = APViTCifar10(
        num_patches=16,
        hidden_channels=hidden_channels,
        attn_embed_dim=attn_embed_dim,
        num_transformer_layers=num_transformer_layers,
        stochastic_depth=stochastic_depth,
        pos_embed_size=4,
        scaling=scaling,
        max_scale=0.4,
        rotating=rotating
    ).to(device)

    ap_criterion = AdaptivePatchLoss(
        lower_quantile=lower_quantile
    )
    vit_criterion = nn.CrossEntropyLoss()
    criterion = [ap_criterion, vit_criterion]

    def param_filter(module, condition):
        return [p for n, p in module.named_parameters() if condition(n)]

    ap_params = param_filter(model.vit.adaptive_patches, lambda n: 'bias' not in n)
    ap_bias_params = param_filter(model.vit.adaptive_patches, lambda n: 'bias' in n)
    vit_params = param_filter(model, lambda n: 'vit.adaptive_patches' not in n and 'bias' not in n)
    vit_bias_params = param_filter(model, lambda n: 'vit.adaptive_patches' not in n and 'bias' in n)
    optimizer = torch.optim.AdamW([
        {'params': ap_params, 'lr': ap_lr, 'weight_decay': ap_weight_decay},
        {'params': ap_bias_params, 'lr': ap_lr, 'weight_decay': 0.0},
        {'params': vit_params, 'lr': lr, 'weight_decay': weight_decay},
        {'params': vit_bias_params, 'lr': lr, 'weight_decay': 0.0}
    ], lr=lr)

    scheduler = CosineAnnealingLR(
        optimizer,
        T_max=epochs-warmup_epochs,
        eta_min=eta_min
    )

    warmup_scheduler = LambdaLR(
        optimizer,
        lr_lambda=lambda epoch: epoch / warmup_epochs
    )

    scaler = GradScaler()

    mixup_fn = Mixup(
        mixup_alpha=mixup_alpha,
        cutmix_alpha=cutmix_alpha,
        prob=mixup_prob,
        switch_prob=mixup_switch_prob,
        mode='batch', label_smoothing=label_smoothing,
        num_classes=10
    )

    best_accuracy = 0.0

    for epoch in range(epochs):
            vit_train_loss, ap_train_loss = train(
                model,
                trainloader,
                criterion,
                optimizer,
                scheduler,
                warmup_scheduler,
                warmup_epochs,
                epoch,
                accumulation_steps,
                scaler,
                mixup_fn,
                ap_loss_weight,
                ema_decay,
                ema,
                ap_loss,
                device
            )
            vit_test_loss, ap_test_loss, accuracy = evaluate(
                model,
                testloader,
                criterion,
                device
            )

            trial.report(accuracy, epoch)

            print(f"Epoch: {epoch + 1}/{epochs} | ViT Train Loss: {vit_train_loss:.4f} | ViT Test Loss: {vit_test_loss:.4f} | Accuracy: {accuracy*100:.2f}% | AP Train Loss: {ap_train_loss:.4f}, AP Test Loss: {ap_test_loss:.4f}, | LR: {optimizer.param_groups[0]['lr']:.6f}")

            if accuracy > best_accuracy:
                best_accuracy = accuracy

    return best_accuracy

def main():
    search_space = {
        "scaling": [None, 'isotropic', 'anisotropic'],
        "rotating": [True, False],
        "ema": [True, False],
        "ap_loss": [True, False],
    }
    sampler = optuna.samplers.GridSampler(search_space)
    study = optuna.create_study(
        direction="maximize",
        sampler=sampler,
        storage="sqlite:///ablation_study.db",
        study_name="Ablation_Study",
        load_if_exists=True
    )
    study.optimize(objective, n_trials=24)

if __name__ == "__main__": main()
